"""Quota + rate-limit + spend-cap enforcement and usage recording."""
from fastapi import HTTPException

from product import store
from product.plans import get_plan

# Events whose monthly quota gates a request. An analyze run also consumes LLM tokens, so it is refused once
# the token quota is spent (tokens are only known after the run, so the check is "any headroom left").
GATES = {"analyze": ("analyze", "llm_tokens"), "cost_check": ("cost_check",)}


def check_rate(tenant: dict) -> None:
    """Sliding 60s window per tenant, kept in the product DB so it holds across workers and restarts."""
    if not store.rate_hit(tenant["id"], get_plan(tenant["plan"]).rate_per_minute):
        raise HTTPException(status_code=429, detail="rate limit exceeded", headers={"Retry-After": "60"})


def _refusal(tenant: dict, used: dict, event: str, qty: int = 1) -> str | None:
    """Why this request must be refused given this month's `used`, or None. Pure, so it runs inside the
    reservation transaction."""
    plan = get_plan(tenant["plan"])
    if plan.hard_limit:
        for e in GATES.get(event, (event,)):
            quota = plan.monthly_quota.get(e)
            if quota is not None and used.get(e, 0) + (qty if e == event else 1) > quota:
                return f"monthly {e} quota exhausted on plan '{tenant['plan']}'"
    elif plan.spend_cap_cents is not None:
        after = {**used, event: used.get(event, 0) + qty}
        if plan.overage_cents(after) > plan.spend_cap_cents:
            return f"monthly spend cap reached on plan '{tenant['plan']}'"
    return None


def check_quota(tenant: dict, event: str) -> None:
    """Early refusal before doing any work. Advisory only: `reserve` is the atomic gate."""
    reason = _refusal(tenant, store.usage_since(tenant["id"], store.month_start()), event)
    if reason:
        raise HTTPException(status_code=429, detail=reason)


def reserve(tenant: dict, event: str, quantity: int = 1) -> int:
    """Atomically check limits and record the unit. Returns the usage row id; `release` it if the work fails."""
    try:
        return store.reserve(tenant["id"], event, quantity, lambda used: _refusal(tenant, used, event, quantity))
    except store.QuotaRefused as e:
        raise HTTPException(status_code=429, detail=str(e))


def release(row_id: int) -> None:
    store.release(row_id)


def record(tenant: dict, event: str, quantity: int = 1) -> None:
    store.record(tenant["id"], event, quantity)


def summary(tenant: dict) -> dict:
    plan = get_plan(tenant["plan"])
    used = store.usage_since(tenant["id"], store.month_start())
    return {"tenant": tenant["id"], "plan": plan.name, "period_start": store.month_start(),
            "usage": {e: {"used": used.get(e, 0), "included": plan.monthly_quota.get(e)} for e in plan.monthly_quota}}
