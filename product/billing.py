"""Stripe usage reporting via Billing Meter events (v1/billing/meter_events), over httpx.

  STRIPE_SECRET_KEY            required to report; unset = usage is only exported (GET /admin/usage)
  STRIPE_METER_<EVENT>         meter event_name per event, e.g. STRIPE_METER_ANALYZE=agent_analyze_runs
Prices/tiers (included units, overage rates) live in Stripe on the meter's price; this layer only
reports quantities. Each batch carries an `identifier`, so a retry never double-bills."""
import os

import httpx

from product import store
from product.plans import EVENTS

URL = "https://api.stripe.com/v1/billing/meter_events"


def report_usage(post=None) -> dict:
    """Send unreported usage rows to Stripe, one aggregated event per tenant+event. Returns counts."""
    key = os.getenv("STRIPE_SECRET_KEY")
    if not key:
        return {"reported": 0, "skipped": "STRIPE_SECRET_KEY is not configured"}
    post = post or (lambda data: httpx.post(URL, data=data, auth=(key, ""), timeout=15))
    sent = failed = 0
    with store.conn() as c:
        groups = c.execute(
            "SELECT u.tenant_id, u.event, SUM(u.quantity) q, MAX(u.id) last_id, t.stripe_customer_id cust "
            "FROM usage u JOIN tenants t ON t.id=u.tenant_id WHERE u.reported=0 GROUP BY u.tenant_id,u.event").fetchall()
        for g in groups:
            meter = os.getenv(f"STRIPE_METER_{g['event'].upper()}")
            if not (g["cust"] and meter and g["event"] in EVENTS):
                continue  # not billable yet (no customer / no meter): stays unreported
            try:
                r = post({"event_name": meter, "payload[stripe_customer_id]": g["cust"],
                          "payload[value]": str(g["q"]), "identifier": f"{g['tenant_id']}-{g['event']}-{g['last_id']}"})
                ok = r.status_code < 300
            except httpx.HTTPError:
                ok = False
            if ok:
                c.execute("UPDATE usage SET reported=1 WHERE tenant_id=? AND event=? AND id<=? AND reported=0",
                          (g["tenant_id"], g["event"], g["last_id"]))
                sent += 1
            else:
                failed += 1
    return {"reported": sent, "failed": failed}
