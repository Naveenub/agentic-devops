"""One real Stripe TEST-mode run of the whole billing path: sync -> checkout -> webhook -> usage report -> cancel.

  STRIPE_SECRET_KEY=sk_test_... python -m product.stripe_e2e [--plan pro] [--keep]     (make stripe-e2e)

Refuses any key that is not a test-mode key. Uses a throwaway SQLite DB, a throwaway customer and a throwaway
tenant; the customer is deleted at the end unless --keep. `stripe_sync --apply` writes products/prices/meters to the
test account (idempotent, same as running `make stripe-sync`).

What is real: every Stripe call, and the event payloads (read back from GET /v1/events). What is not: Checkout's hosted
page cannot be paid headlessly, so the session is created for real (URL checked) and the subscription it would create is
made through the API with the same prices/metadata and the test card pm_card_visa; events then reach POST /stripe/webhook
through the real route, signed with a local secret. checkout.session.completed is therefore not exercised: complete that
one by hand in a browser (card 4242 4242 4242 4242) with `stripe listen --forward-to localhost:8000/stripe/webhook`.
"""
import hashlib
import hmac
import json
import os
import sys
import tempfile
import time

import httpx

from product import store, subscriptions as subs
from product.plans import METERS, get_plan

LOCAL_WHSEC = "whsec_local_e2e"
WATCH = ("customer.subscription.created", "customer.subscription.updated", "customer.subscription.deleted",
         "invoice.payment_failed")
results: list[tuple[str, bool, str]] = []


def step(name: str, ok: bool, detail: str = "") -> bool:
    results.append((name, ok, detail))
    print(f"{'PASS' if ok else 'FAIL'}  {name}" + (f"  ({detail})" if detail else ""), flush=True)
    return ok


def api(method: str, path: str, data: dict | None = None):
    r = httpx.request(method, f"{subs.API}{path}", auth=(os.environ["STRIPE_SECRET_KEY"], ""), timeout=30,
                      **({"params": data} if method == "GET" else {"data": data}))
    if r.status_code >= 300:
        raise subs.StripeError(f"{method} {path} -> {r.status_code}: {r.text[:300]}")
    return r.json()


def preflight(call=None) -> str | None:
    """Cheap authenticated call before anything is written: a bad key, no network, or an account without Billing
    Meters fails here with one clear line instead of halfway through the run. None = fine."""
    try:
        (call or api)("GET", "/billing/meters", {"limit": 1})
    except httpx.HTTPError as e:
        return f"Stripe unreachable: {e}"
    except subs.StripeError as e:
        return f"key rejected or Billing Meters unavailable on this account: {e}"
    return None


def poll(fn, timeout: float = 60, every: float = 3):
    end = time.time() + timeout
    while True:
        out = fn()
        if out or time.time() > end:
            return out
        time.sleep(every)


def deliver(client, event: dict) -> str:
    body = json.dumps(event).encode()
    ts = str(int(time.time()))
    sig = hmac.new(LOCAL_WHSEC.encode(), f"{ts}.".encode() + body, hashlib.sha256).hexdigest()
    r = client.post("/stripe/webhook", content=body, headers={"Stripe-Signature": f"t={ts},v1={sig}"})
    return f"{r.status_code}:{r.json().get('result', r.json().get('detail'))}" if r.headers.get(
        "content-type", "").startswith("application/json") else str(r.status_code)


def our_events(cust: str, seen: set) -> list[dict]:
    rows = api("GET", "/events", {"limit": 100, "types[]": list(WATCH), "created[gte]": int(START)})["data"]
    out = [e for e in reversed(rows) if e["id"] not in seen and e["data"]["object"].get("customer") == cust]
    seen.update(e["id"] for e in out)
    return out


def run(plan: str, keep: bool) -> None:
    global START
    START = time.time() - 5
    from fastapi.testclient import TestClient
    from api.main import app
    from product import billing, stripe_sync

    # 1. sync: create, then re-check; a second pass must report no drift against real Stripe objects
    drift, env = stripe_sync.sync(apply=True)
    os.environ.update(env)
    step("sync --apply", f"STRIPE_PRICE_{plan.upper()}" in env, f"{len(env)} env vars; changed: {len(drift)}")
    drift, _ = stripe_sync.sync(apply=False)
    step("sync re-check shows no drift", not drift, "; ".join(drift)[:300])
    if not subs.purchasable(plan):
        step(f"plan {plan} purchasable", False, "no price resolved"); return

    # 2. checkout
    tenant = store.create_tenant("e2e-" + str(int(time.time())))
    url = subs.create_checkout(tenant, plan, "https://example.com/ok", "https://example.com/cancel")
    step("checkout session created", url.startswith("https://checkout.stripe.com/"), url[:60])
    tenant = store.get_tenant(tenant["id"])
    cust = tenant["stripe_customer_id"]
    step("customer stored on tenant", bool(cust) and cust.startswith("cus_"), cust or "")
    try:
        pm = api("POST", "/payment_methods/pm_card_visa/attach", {"customer": cust})["id"]
        api("POST", f"/customers/{cust}", {"invoice_settings[default_payment_method]": pm})
        form = {"customer": cust, "metadata[tenant_id]": tenant["id"], "metadata[plan]": plan}
        for i, (pid, _) in enumerate(subs.prices_for(plan)):
            form[f"items[{i}][price]"] = pid
        sub = api("POST", "/subscriptions", form)
        step("test subscription created", sub["status"] in subs.GOOD, f"{sub['id']} {sub['status']}")

        # 3. webhook: real events, real route, real signature check
        os.environ["STRIPE_WEBHOOK_SECRET"] = LOCAL_WHSEC
        client, seen = TestClient(app), set()
        got = poll(lambda: [e for e in our_events(cust, seen) if e["type"].startswith("customer.subscription")] or None)
        events = got or []
        step("subscription events arrived from Stripe", bool(events), ", ".join(e["type"] for e in events))
        done = [deliver(client, e) for e in events]
        step("webhook accepted events (200)", bool(done) and all(d.startswith("200:") for d in done), " ".join(done))
        t = store.get_tenant(tenant["id"])
        step(f"tenant moved to {plan}", (t["plan"], t["stripe_subscription_id"], t["subscription_status"]) ==
             (plan, sub["id"], "active"), f"{t['plan']} / {t['subscription_status']}")
        step("replayed event is a duplicate", deliver(client, events[0]).endswith("duplicate") if events else False)
        bad = client.post("/stripe/webhook", content=b"{}", headers={"Stripe-Signature": "t=1,v1=00"})
        step("bad signature rejected (400)", bad.status_code == 400, str(bad.status_code))

        # 4. usage report -> meter events -> Stripe's own aggregation
        n = {"analyze": 7, "cost_check": 3, "llm_tokens": 12345}
        for ev, q in n.items():
            store.record(tenant["id"], ev, q)
        res = billing.report_usage()
        step("usage reported", res.get("reported") == len(n) and not res.get("failed"), json.dumps(res))
        step("second report is a no-op", billing.report_usage().get("reported") == 0)
        meters = {m["event_name"]: m["id"] for m in api("GET", "/billing/meters", {"limit": 100})["data"]}
        for ev, q in n.items():
            mid = meters[METERS[ev]]
            end = int(time.time() // 60 * 60) + 60
            def total(mid=mid, end=end):
                rows = api("GET", f"/billing/meters/{mid}/event_summaries",
                           {"customer": cust, "start_time": int(START // 60 * 60), "end_time": end})["data"]
                return sum(float(r["aggregated_value"]) for r in rows) or None
            seen_total = poll(total, timeout=90, every=5)
            step(f"Stripe meter {METERS[ev]} aggregated", seen_total == q, f"want {q}, Stripe {seen_total}")
        # ponytail: invoice amount not asserted - meter summaries prove ingestion; add when tiers are re-priced

        # 5. cancel -> deleted event -> downgrade
        api("DELETE", f"/subscriptions/{sub['id']}")
        got = poll(lambda: [e for e in our_events(cust, seen) if e["type"] == "customer.subscription.deleted"] or None)
        done = [deliver(client, e) for e in got or []]
        t = store.get_tenant(tenant["id"])
        step("cancel downgrades tenant to free", bool(done) and t["plan"] == "free", f"{done} -> {t['plan']}")
    finally:
        if not keep:
            try:
                api("DELETE", f"/customers/{cust}")
            except Exception as e:  # noqa: BLE001
                print(f"cleanup: could not delete {cust}: {e}")


def main(argv=None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    key = os.getenv("STRIPE_SECRET_KEY", "")
    if not key.startswith(("sk_test_", "rk_test_")):
        print("refusing to run: STRIPE_SECRET_KEY must be a test-mode key (sk_test_... / rk_test_...)")
        return 2
    plan = argv[argv.index("--plan") + 1] if "--plan" in argv else "pro"
    get_plan(plan)
    if err := preflight():
        print(f"refusing to run: {err}")
        return 2
    tmp = tempfile.mkdtemp()
    os.environ.update({"PRODUCT_DB": os.path.join(tmp, "e2e.db"), "AGENT_LLM": "off", "PRODUCT_ADMIN_KEY": "e2e"})
    try:
        run(plan, "--keep" in argv)
    except Exception as e:  # noqa: BLE001
        step("run aborted", False, f"{type(e).__name__}: {e}")
    bad = [r for r in results if not r[1]]
    print(f"\n{len(results) - len(bad)}/{len(results)} steps passed")
    if not bad:
        try:  # the Phase 0 ship gate (make ship-gates) reads this; a write failure must not turn a pass into a fail
            from product import ship_gates
            ship_gates.record("stripe_e2e", plan=plan, steps=len(results))
        except Exception as e:  # noqa: BLE001
            print(f"warning: could not record the run for make ship-gates: {type(e).__name__}: {e}")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
