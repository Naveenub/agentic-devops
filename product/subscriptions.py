"""Stripe customer + subscription lifecycle: customers, Checkout, billing portal and webhooks.
Plain httpx against the Stripe REST API (same style as billing.py); no SDK.

  STRIPE_SECRET_KEY        required for every call here
  STRIPE_WEBHOOK_SECRET    endpoint signing secret (whsec_...); unset = POST /stripe/webhook returns 503
  STRIPE_PRICE_<PLAN>      Checkout price id(s) for a paid plan, comma-separated, e.g. STRIPE_PRICE_PRO=price_base,price_usage:metered,price_seat:seat
                           A ":metered" suffix marks usage-based prices (Checkout takes no quantity for them). A ":seat" suffix
                           marks the per-member price: its quantity is the tenant's active member count, set at Checkout and
                           kept current by sync_seats (a plan with seat_cents but no seat price cannot be bought, so seats are
                           never silently free).
  STRIPE_SUCCESS_URL / STRIPE_CANCEL_URL / STRIPE_PORTAL_RETURN_URL   where Stripe sends the customer back

Lifecycle policy: a subscription that is active/trialing puts the tenant on the plan its price maps to.
the first failed payment (invoice.payment_failed, or status past_due) drops the tenant to `free` at once but
keeps the subscription id, so Stripe's retries continue and a successful retry restores the paid plan.
canceled, unpaid and incomplete_expired also drop to `free` and clear the subscription. The tenant stays active."""
import hashlib
import hmac
import json
import logging
import os
import time

import httpx

from product import store
from product.plans import get_plan

log = logging.getLogger("product.subscriptions")
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


def _price_entries(plan: str) -> list[tuple[str, str]]:
    out = []
    for part in os.getenv(f"STRIPE_PRICE_{plan.upper()}", "").split(","):
        part = part.strip()
        if part:
            pid, _, flag = part.partition(":")
            out.append((pid, flag))
    return out


def prices_for(plan: str) -> list[tuple[str, bool]]:
    """[(price_id, metered)] for the plan's base and usage prices; empty when the plan is not purchasable.
    The per-seat price is separate (seat_price): its quantity is not 1."""
    return [(pid, flag == "metered") for pid, flag in _price_entries(plan) if flag != "seat"]


def seat_price(plan: str) -> str | None:
    """The Stripe price id of the plan's per-member price, if one is configured."""
    return next((pid for pid, flag in _price_entries(plan) if flag == "seat"), None)


def purchasable(plan: str) -> bool:
    """True when Stripe is configured and the plan has a Checkout price (never for `free`)."""
    return plan != "free" and bool(os.getenv("STRIPE_SECRET_KEY")) and bool(prices_for(plan))


def plan_for_prices(price_ids: list[str]) -> str | None:
    from product.plans import _load
    for name in _load():
        if any(pid in price_ids for pid, _ in prices_for(name)):
            return name
    return None  # a seat price alone never identifies a plan: the base price does


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
    seats = get_plan(plan).seat_cents
    if seats and not seat_price(plan):
        raise StripeError(f"plan {plan!r} charges per seat but STRIPE_PRICE_{plan.upper()} has no :seat price; "
                          "run `python -m product.stripe_sync --apply` and set the env it prints", 422)
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
    if seats:  # one seat per active member (the owner included), so a team that already has members pays for them
        data[f"line_items[{len(prices)}][price]"] = seat_price(plan)
        data[f"line_items[{len(prices)}][quantity]"] = str(store.seat_count(tenant["id"]))
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
        store.set_seats_synced(t["id"], _seat_quantity(sub, plan))
    elif status in DEAD:
        _downgrade(t, status)
    else:  # past_due, incomplete, paused: record why; a failing payment loses the paid plan immediately
        if status == "past_due":
            fields["plan"] = FALLBACK_PLAN
        store.update_tenant(t["id"], stripe_subscription_id=sid, subscription_status=status, **fields)


def _seat_quantity(sub: dict, plan: str) -> int | None:
    """Quantity Stripe currently bills for the plan's seat price on this subscription; None if there is none."""
    pid = seat_price(plan)
    for item in (sub.get("items") or {}).get("data", []):
        if pid and (item.get("price") or {}).get("id") == pid:
            return item.get("quantity")
    return None


def _downgrade(t: dict, status: str) -> None:
    store.update_tenant(t["id"], plan=FALLBACK_PLAN)
    store.clear_subscription(t["id"], status)


def _invoice_subscription(inv: dict) -> str | None:
    """Subscription id of an invoice. Older Stripe API versions put it at `subscription`; newer ones
    (2025-03-31 and later) at `parent.subscription_details.subscription`."""
    sid = inv.get("subscription")
    if isinstance(sid, dict):
        sid = sid.get("id")
    if sid:
        return sid
    details = ((inv.get("parent") or {}).get("subscription_details") or {})
    sid = details.get("subscription")
    return sid.get("id") if isinstance(sid, dict) else sid


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
        if t and _invoice_subscription(obj) in (None, t.get("stripe_subscription_id")):
            if obj.get("next_payment_attempt") is None:  # Stripe has given up retrying
                _downgrade(t, "unpaid")
            else:  # downgrade now, keep the subscription: a successful retry re-upgrades via subscription.updated
                store.update_tenant(t["id"], plan=FALLBACK_PLAN, subscription_status="past_due")
        result = "handled"
    else:
        return "ignored"
    with store.conn() as c:
        c.execute("INSERT INTO stripe_events(id,type,received_at) VALUES(?,?,?) ON CONFLICT DO NOTHING",
                  (eid, typ, time.time()))
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


# ---- seats -------------------------------------------------------------------------------------------

def sync_seats(tid: str, post=None) -> dict:
    """Make Stripe bill the tenant's current active member count on the plan's seat price. Sets an absolute
    quantity (never an increment), so it is idempotent and safe to repeat or to run from two places at once; it
    re-reads the count after writing and goes round again if a member changed meanwhile. Changes are prorated.
    Returns {"status": "synced"|"unchanged"|"skipped", ...}. Raises StripeError; callers that must not fail on
    Stripe being down use sync_seats_quietly, and reconcile_seats retries what was missed."""
    t = store.get_tenant(tid)
    if not t or not os.getenv("STRIPE_SECRET_KEY"):
        return {"status": "skipped", "reason": "no tenant or STRIPE_SECRET_KEY"}
    sid = t.get("stripe_subscription_id")
    pid = seat_price(t["plan"]) if get_plan(t["plan"]).seat_cents else None
    if not sid or t.get("subscription_status") in DEAD or not pid:
        return {"status": "skipped", "reason": "no subscription with a seat price"}
    result = {"status": "unchanged"}
    for _ in range(3):
        want = store.seat_count(tid)
        item = next((i for i in (_fetch_subscription(sid).get("items") or {}).get("data", [])
                     if (i.get("price") or {}).get("id") == pid), None)
        if item and item.get("quantity") == want:
            store.set_seats_synced(tid, want)
        else:
            if item:
                _post(f"/subscription_items/{item['id']}", {"quantity": str(want), "proration_behavior": "create_prorations"},
                      post=post)
            else:  # the subscription predates seat pricing (or a plan change dropped the item): add it
                _post("/subscription_items", {"subscription": sid, "price": pid, "quantity": str(want),
                                              "proration_behavior": "create_prorations"}, post=post)
            store.set_seats_synced(tid, want)
            result = {"status": "synced", "seats": want}
        if store.seat_count(tid) == want:
            return {**result, "seats": want}
    return {**result, "seats": store.seat_count(tid), "note": "members were changing; reconcile will settle it"}


def sync_seats_quietly(tid: str) -> None:
    """For background tasks after a member change: the change has already committed, so a Stripe failure is
    logged and left for reconcile_seats (seats_synced then differs from the member count)."""
    try:
        sync_seats(tid)
    except Exception as e:  # noqa: BLE001 - network, Stripe and DB errors alike
        log.warning("seat sync failed for %s: %s", tid, type(e).__name__)


def reconcile_seats() -> dict:
    """Re-sync every paid tenant whose billed seat count is not the member count. Run by the billing scheduler."""
    if not os.getenv("STRIPE_SECRET_KEY"):
        return {"seats_synced": 0}
    done = failed = 0
    for d in store.seat_drift():
        try:
            r = sync_seats(d["tenant_id"])
            done += r["status"] in ("synced", "unchanged")
        except StripeError as e:
            failed += 1
            log.warning("seat reconcile failed for %s: %s", d["tenant_id"], e)
    return {"seats_synced": done, "seats_failed": failed}
