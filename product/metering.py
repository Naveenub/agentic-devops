"""Quota + rate-limit enforcement and usage recording."""
import threading
import time
from collections import defaultdict, deque

from fastapi import HTTPException

from product import store
from product.plans import get_plan

_hits: dict = defaultdict(deque)
_lock = threading.Lock()


def check_rate(tenant: dict) -> None:
    """Sliding 60s window per tenant, in-process (run one worker, or front with a shared limiter)."""
    limit = get_plan(tenant["plan"]).rate_per_minute
    now = time.monotonic()
    with _lock:
        q = _hits[tenant["id"]]
        while q and now - q[0] > 60:
            q.popleft()
        if len(q) >= limit:
            raise HTTPException(status_code=429, detail="rate limit exceeded", headers={"Retry-After": "60"})
        q.append(now)


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
