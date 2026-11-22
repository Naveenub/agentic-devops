"""Quota + rate-limit enforcement and usage recording."""
from fastapi import HTTPException

from product import store
from product.plans import get_plan

def check_rate(tenant: dict) -> None:
    """Sliding 60s window per tenant, kept in the product DB so it holds across workers and restarts."""
    if not store.rate_hit(tenant["id"], get_plan(tenant["plan"]).rate_per_minute):
        raise HTTPException(status_code=429, detail="rate limit exceeded", headers={"Retry-After": "60"})


def check_quota(tenant: dict, event: str) -> None:
    """Hard-limit plans only: refuse before doing the work once the month's units are used up."""
    plan = get_plan(tenant["plan"])
    quota = plan.monthly_quota.get(event)
    if quota is None or not plan.hard_limit:
        return
    used = store.usage_since(tenant["id"], store.month_start()).get(event, 0)
    if used >= quota:
        raise HTTPException(status_code=429, detail=f"monthly {event} quota exhausted on plan '{tenant['plan']}'")


def record(tenant: dict, event: str, quantity: int = 1) -> None:
    store.record(tenant["id"], event, quantity)


def summary(tenant: dict) -> dict:
    plan = get_plan(tenant["plan"])
    used = store.usage_since(tenant["id"], store.month_start())
    return {"tenant": tenant["id"], "plan": plan.name, "period_start": store.month_start(),
            "usage": {e: {"used": used.get(e, 0), "included": plan.monthly_quota.get(e)} for e in plan.monthly_quota}}
