"""Built-in billing scheduler: reports usage to Stripe on an interval, so no external cron is needed.

  PRODUCT_BILLING_INTERVAL   seconds between reports, e.g. 3600. Unset or 0 = off (run `python -m product.billing`
                             from cron instead, or POST /admin/billing/report by hand).

Every worker and replica runs this loop, but each tick first claims the job in the shared database
(store.claim_run), so exactly one of them reports per interval. A report that fails is retried at the next
interval; Stripe de-duplicates by event identifier, so a retry never double-bills."""
import asyncio
import logging
import os

from product import billing, store

JOB = "billing-report"
log = logging.getLogger(__name__)


def interval() -> float:
    """Configured seconds between reports; 0 when disabled or not a positive number."""
    try:
        return max(float(os.getenv("PRODUCT_BILLING_INTERVAL") or 0), 0.0)
    except ValueError:
        log.warning("PRODUCT_BILLING_INTERVAL is not a number; billing scheduler is off")
        return 0.0


def run_due(every: float, report=None) -> dict | None:
    """Run the report if it is due and this caller wins the claim. Returns its counts, or None if not due."""
    if not store.claim_run(JOB, every):
        return None
    return (report or billing.report_usage)()


async def billing_forever(every: float, report=None) -> None:
    """Wake up often enough to notice a due report promptly, whatever the interval is."""
    tick = min(every, 60.0)
    while True:
        try:
            result = await asyncio.to_thread(run_due, every, report)  # report_usage blocks on HTTP
            if result and (result.get("reported") or result.get("failed")):
                log.info("billing report: %s", result)
        except asyncio.CancelledError:
            raise
        except Exception:  # the loop must outlive a bad run (DB blip, Stripe outage)
            log.exception("billing report failed")
        await asyncio.sleep(tick)
