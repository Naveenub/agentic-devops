"""Stripe usage reporting via Billing Meter events (v1/billing/meter_events), over httpx.

  STRIPE_SECRET_KEY            required to report; unset = usage is only exported (GET /admin/usage)
  STRIPE_METER_<EVENT>         meter event_name per event, must equal plans.METERS, e.g. STRIPE_METER_ANALYZE=agent_analyze_runs
Prices/tiers come from product/plans.py (provisioned and checked by product/stripe_sync.py); this layer only
reports quantities. Each batch carries an `identifier`, so a retry never double-bills."""
import os

import httpx

from product import store
from product.plans import EVENTS

URL = "https://api.stripe.com/v1/billing/meter_events"


def report_usage(post=None) -> dict:
    """Send unreported usage rows to Stripe, one aggregated event per tenant+event+UTC month, stamped with the
    time of the latest row, so a late run bills each row into the month it happened in. Returns counts.
    (Stripe rejects timestamps older than 35 days; such a batch stays unreported and counts as failed.)
    Safe to run from several places at once (cron, the built-in scheduler, the admin endpoint): the identifier on
    each event makes Stripe drop a duplicate. No database transaction is held open while talking to Stripe."""
    key = os.getenv("STRIPE_SECRET_KEY")
    if not key:
        return {"reported": 0, "skipped": "STRIPE_SECRET_KEY is not configured"}
    post = post or (lambda data: httpx.post(URL, data=data, auth=(key, ""), timeout=15))
    sent = failed = 0
    ym_sql = store.month_sql("u.ts")
    with store.conn() as c:
        groups = [dict(g) for g in c.execute(
            f"SELECT u.tenant_id, u.event, {ym_sql} ym, CAST(SUM(u.quantity) AS BIGINT) q, MAX(u.id) last_id, "
            "MAX(u.ts) last_ts, MAX(t.stripe_customer_id) cust "
            f"FROM usage u JOIN tenants t ON t.id=u.tenant_id WHERE u.reported=0 "
            f"GROUP BY u.tenant_id,u.event,{ym_sql}").fetchall()]
    for g in groups:
        meter = os.getenv(f"STRIPE_METER_{g['event'].upper()}")
        if not (g["cust"] and meter and g["event"] in EVENTS):
            continue  # not billable yet (no customer / no meter): stays unreported
        try:
            r = post({"event_name": meter, "payload[stripe_customer_id]": g["cust"],
                      "payload[value]": str(g["q"]), "timestamp": str(int(g["last_ts"])),
                      "identifier": f"{g['tenant_id']}-{g['event']}-{g['last_id']}"})
            ok = r.status_code < 300
        except httpx.HTTPError:
            ok = False
        if ok:
            with store.conn() as c:
                c.execute("UPDATE usage SET reported=1 WHERE tenant_id=? AND event=? AND id<=? AND reported=0 "
                          f"AND {store.month_sql('ts')}=?", (g["tenant_id"], g["event"], g["last_id"], g["ym"]))
            sent += 1
        else:
            failed += 1
    return {"reported": sent, "failed": failed}


def main() -> int:
    """`python -m product.billing`: one report run, for an external scheduler (cron, a Kubernetes CronJob)
    if you prefer that to PRODUCT_BILLING_INTERVAL. Prints the counts; exit status 1 if any batch failed."""
    import json

    from product import subscriptions
    result = report_usage()
    seats = subscriptions.reconcile_seats()
    if seats.get("seats_synced") or seats.get("seats_failed"):
        result = {**result, **seats}
    print(json.dumps(result))
    return 1 if result.get("failed") or result.get("seats_failed") else 0


if __name__ == "__main__":
    raise SystemExit(main())
