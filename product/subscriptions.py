"""Stripe customer + subscription lifecycle: customers, Checkout, billing portal and webhooks.
Plain httpx against the Stripe REST API (same style as billing.py); no SDK.

  STRIPE_SECRET_KEY        required for every call here
  STRIPE_WEBHOOK_SECRET    endpoint signing secret (whsec_...); unset = POST /stripe/webhook returns 503
  STRIPE_PRICE_<PLAN>      Checkout price id(s) for a paid plan, comma-separated, e.g. STRIPE_PRICE_PRO=price_base,price_usage:metered
                           A ":metered" suffix marks usage-based prices (Checkout takes no quantity for them).
  STRIPE_SUCCESS_URL / STRIPE_CANCEL_URL / STRIPE_PORTAL_RETURN_URL   where Stripe sends the customer back

Lifecycle policy: a subscription that is active/trialing puts the tenant on the plan its price maps to.
past_due keeps the plan while Stripe retries the card (dunning). canceled, unpaid, incomplete_expired
(or a failed invoice with no retry left) drops the tenant to `free`; the tenant stays active."""
import hashlib
import hmac
import json
import os
import time

import httpx

from product import store
from product.plans import get_plan

API = "https://api.stripe.com/v1"
TOLERANCE = 300  # seconds a webhook signature stays valid
FALLBACK_PLAN = "free"
GOOD = {"active", "trialing"}
DEAD = {"canceled", "unpaid", "incomplete_expired"}


class StripeError(Exception):
    def __init__(self, msg: str, status: int = 502):
        super().__init__(msg)
        self.status = status


def _key() -> str:
    key = os.getenv("STRIPE_SECRET_KEY")
    if not key:
        raise StripeError("STRIPE_SECRET_KEY is not configured", 503)
    return key


def _post(path: str, data: dict, idem: str | None = None, post=None) -> dict:
    key = _key()
    headers = {"Idempotency-Key": idem} if idem else {}
    post = post or (lambda url, d, h: httpx.post(url, data=d, headers=h, auth=(key, ""), timeout=15))
    try:
        r = post(f"{API}{path}", data, headers)
    except httpx.HTTPError as e:
        raise StripeError(f"Stripe unreachable: {e}")
    if r.status_code >= 300:
        raise StripeError(f"Stripe error {r.status_code}", 502)
    return r.json()


def prices_for(plan: str) -> list[tuple[str, bool]]:
    """[(price_id, metered)] configured for a plan; empty when the plan is not purchasable."""
    out = []
    for part in os.getenv(f"STRIPE_PRICE_{plan.upper()}", "").split(","):
        part = part.strip()
        if part:
            pid, _, flag = part.partition(":")
            out.append((pid, flag == "metered"))
    return out


def plan_for_prices(price_ids: list[str]) -> str | None:
    from product.plans import _load
    for name in _load():
        if any(pid in price_ids for pid, _ in prices_for(name)):
            return name
    return None


def ensure_customer(tenant: dict, post=None) -> str:
    """Create the Stripe customer once. The idempotency key stops two concurrent calls making two customers."""
    if tenant.get("stripe_customer_id"):
        return tenant["stripe_customer_id"]
    cust = _post("/customers", {"name": tenant["name"], "metadata[tenant_id]": tenant["id"]},
                 idem=f"customer-{tenant['id']}", post=post)["id"]
    store.update_tenant(tenant["id"], stripe_customer_id=cust)
    return cust


def create_checkout(tenant: dict, plan: str, success_url: str | None = None, cancel_url: str | None = None,
                    post=None) -> str:
    prices = prices_for(plan)
    if not prices:
        raise StripeError(f"plan {plan!r} cannot be purchased (no STRIPE_PRICE_{plan.upper()})", 422)
    get_plan(plan)
    if tenant.get("stripe_subscription_id") and tenant.get("subscription_status") not in DEAD:
        raise StripeError("tenant already has a subscription; change it through the billing portal", 409)
    cust = ensure_customer(tenant, post=post)
    data = {"mode": "subscription", "customer": cust, "client_reference_id": tenant["id"],
            "success_url": success_url or os.getenv("STRIPE_SUCCESS_URL", ""),
            "cancel_url": cancel_url or os.getenv("STRIPE_CANCEL_URL", ""),
            "subscription_data[metadata][tenant_id]": tenant["id"],
            "subscription_data[metadata][plan]": plan, "metadata[tenant_id]": tenant["id"]}
    if not data["success_url"] or not data["cancel_url"]:
        raise StripeError("success_url and cancel_url are required (or set STRIPE_SUCCESS_URL / STRIPE_CANCEL_URL)", 422)
    for i, (pid, metered) in enumerate(prices):
        data[f"line_items[{i}][price]"] = pid
        if not metered:
            data[f"line_items[{i}][quantity]"] = "1"
    return _post("/checkout/sessions", data, post=post)["url"]


def create_portal(tenant: dict, return_url: str | None = None, post=None) -> str:
    if not tenant.get("stripe_customer_id"):
        raise StripeError("tenant has no Stripe customer yet; start with checkout", 409)
    url = return_url or os.getenv("STRIPE_PORTAL_RETURN_URL") or os.getenv("STRIPE_SUCCESS_URL", "")
    if not url:
        raise StripeError("return_url is required (or set STRIPE_PORTAL_RETURN_URL)", 422)
    return _post("/billing_portal/sessions", {"customer": tenant["stripe_customer_id"], "return_url": url},
                 post=post)["url"]


# ---- webhooks ----------------------------------------------------------------------------------

def verify_signature(body: bytes, header: str | None, secret: str, now: float | None = None) -> bool:
    """Stripe-Signature: t=<ts>,v1=<hmac>[,v1=...]; HMAC-SHA256 over '<ts>.<body>'. Rejects stale timestamps."""
    try:
        parts = [p.split("=", 1) for p in (header or "").split(",")]
        ts = next(v for k, v in parts if k == "t")
        sigs = [v for k, v in parts if k == "v1"]
        age = abs((now or time.time()) - int(ts))
    except (StopIteration, ValueError):
        return False
    if age > TOLERANCE:
        return False
    expected = hmac.new(secret.encode(), f"{ts}.".encode() + body, hashlib.sha256).hexdigest()
    return any(hmac.compare_digest(expected, s) for s in sigs)


def _tenant_for(obj: dict) -> dict | None:
    tid = (obj.get("metadata") or {}).get("tenant_id") or obj.get("client_reference_id")
    t = store.get_tenant(tid) if tid else None
    return t or (store.tenant_for_customer(obj["customer"]) if obj.get("customer") else None)


def _apply_subscription(sub: dict) -> None:
    t = _tenant_for(sub)
    if not t:
        return
    sid, status = sub.get("id"), sub.get("status")
    if t.get("stripe_subscription_id") and t["stripe_subscription_id"] != sid and t.get("subscription_status") not in DEAD:
        # An older subscription's event arriving after the tenant moved to a new one must not overwrite it.
        if status in DEAD:
            return
    fields = {"stripe_customer_id": sub.get("customer")}
    if status in GOOD:
        prices = [i.get("price", {}).get("id") for i in (sub.get("items") or {}).get("data", [])]
        plan = plan_for_prices(prices) or (sub.get("metadata") or {}).get("plan")
        try:
            get_plan(plan)
        except KeyError:
            raise ValueError(f"subscription {sid} maps to unknown plan {plan!r}")
        fields.update(plan=plan, stripe_subscription_id=sid, subscription_status=status)
        store.update_tenant(t["id"], **fields)
    elif status in DEAD:
        _downgrade(t, status)
    else:  # past_due, incomplete, paused: keep the current plan, record why
        store.update_tenant(t["id"], stripe_subscription_id=sid, subscription_status=status,
                            stripe_customer_id=sub.get("customer"))


def _downgrade(t: dict, status: str) -> None:
    store.update_tenant(t["id"], plan=FALLBACK_PLAN)
    store.clear_subscription(t["id"], status)


def handle_event(event: dict) -> str:
    """Apply one verified event. Returns 'duplicate', 'ignored' or 'handled'. Raises on failure so the
    caller answers 5xx and Stripe retries; the event id is only recorded once it has been applied."""
    eid, typ, obj = event["id"], event["type"], event["data"]["object"]
    with store.conn() as c:
        if c.execute("SELECT 1 FROM stripe_events WHERE id=?", (eid,)).fetchone():
            return "duplicate"
    if typ == "checkout.session.completed":
        t = _tenant_for(obj)
        if not t:
            return "ignored"
        store.update_tenant(t["id"], stripe_customer_id=obj.get("customer"))
        if obj.get("subscription"):
            store.update_tenant(t["id"], stripe_subscription_id=obj["subscription"])
            sub = _fetch_subscription(obj["subscription"])
            if sub:
                _apply_subscription(sub)
        result = "handled"
    elif typ in ("customer.subscription.created", "customer.subscription.updated"):
        _apply_subscription(obj)
        result = "handled"
    elif typ == "customer.subscription.deleted":
        t = _tenant_for(obj)
        if t and t.get("stripe_subscription_id") in (None, obj.get("id")):
            _downgrade(t, "canceled")
        result = "handled"
    elif typ == "invoice.payment_failed":
        t = _tenant_for(obj)
        if t and obj.get("subscription") in (None, t.get("stripe_subscription_id")):
            if obj.get("next_payment_attempt") is None:  # Stripe has given up retrying
                _downgrade(t, "unpaid")
            else:
                store.update_tenant(t["id"], subscription_status="past_due")
        result = "handled"
    else:
        return "ignored"
    with store.conn() as c:
        c.execute("INSERT OR IGNORE INTO stripe_events(id,type,received_at) VALUES(?,?,?)", (eid, typ, time.time()))
    return result


def _fetch_subscription(sid: str) -> dict | None:
    """checkout.session.completed carries only the subscription id; read the subscription for its price/status."""
    try:
        key = _key()
        r = httpx.get(f"{API}/subscriptions/{sid}", auth=(key, ""), timeout=15)
    except (StripeError, httpx.HTTPError):
        raise StripeError("could not read subscription")
    if r.status_code >= 300:
        raise StripeError(f"Stripe error {r.status_code}")
    return r.json()


def parse_event(body: bytes) -> dict:
    return json.loads(body)
