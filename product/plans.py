"""Plans, monthly quotas and metered prices. Quotas are hard limits (HTTP 429); prices are what the
billing layer reports to Stripe. Override any value without code via PRODUCT_PLANS (JSON)."""
import json
import os
from dataclasses import dataclass, replace

EVENTS = ("analyze", "cost_check", "llm_tokens")


@dataclass(frozen=True)
class Plan:
    name: str
    monthly_quota: dict  # event -> included units per calendar month (UTC); None = unlimited
    rate_per_minute: int  # requests per minute per tenant
    shared_tools: bool = False  # may use the server's kubectl/GitHub/AWS credentials (single-org deployments)
    hard_limit: bool = False  # True: 429 at the quota. False: usage past the quota is billed as overage


PLANS = {
    "free": Plan("free", {"analyze": 50, "cost_check": 0, "llm_tokens": 100_000}, 10, hard_limit=True),
    "starter": Plan("starter", {"analyze": 1_000, "cost_check": 100, "llm_tokens": 2_000_000}, 60),
    "pro": Plan("pro", {"analyze": 10_000, "cost_check": 1_000, "llm_tokens": 20_000_000}, 300),
    "enterprise": Plan("enterprise", {"analyze": None, "cost_check": None, "llm_tokens": None}, 1_000),
}


def _load() -> dict:
    raw = os.getenv("PRODUCT_PLANS")
    if not raw:
        return PLANS
    plans = dict(PLANS)
    for name, cfg in json.loads(raw).items():
        base = plans.get(name) or Plan(name, {}, 60)
        quota = {**base.monthly_quota, **cfg.get("monthly_quota", {})}
        plans[name] = replace(base, monthly_quota=quota,
                              rate_per_minute=cfg.get("rate_per_minute", base.rate_per_minute),
                              shared_tools=cfg.get("shared_tools", base.shared_tools),
                              hard_limit=cfg.get("hard_limit", base.hard_limit))
    return plans


def get_plan(name: str) -> Plan:
    plans = _load()
    if name not in plans:
        raise KeyError(f"unknown plan {name!r}")
    return plans[name]
