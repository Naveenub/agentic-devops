"""Single source of truth for plans: included units, prices and overage rates.

Stripe is provisioned from this file and checked against it (`python -m product.stripe_sync`, or
GET /admin/billing/drift), so a plan is never defined in two places. Quotas are hard limits (HTTP 429) on
`hard_limit` plans; on the others, usage past the quota is billed at `overage`.

  base_cents   flat monthly price in the currency's minor unit; None = not sold online (e.g. enterprise)
  spend_cap_cents  paid plans: max overage charge per calendar month (minor units); usage that would pass it is
               refused with HTTP 429. None = no cap.
  overage      event -> price per unit past the included quota, in minor units (decimal string, e.g. "0.0002"
               cents per LLM token = $2 per 1M tokens). Events without a rate are not billed.

Override any value without code via PRODUCT_PLANS (JSON); re-run the sync after changing prices or quotas."""
import json
import os
from dataclasses import dataclass, field, replace
from decimal import Decimal

EVENTS = ("analyze", "cost_check", "llm_tokens")

# Stripe meter event_name per event (STRIPE_METER_<EVENT> must equal these; the sync creates the meters).
METERS = {"analyze": "agent_analyze_runs", "cost_check": "agent_cost_checks", "llm_tokens": "agent_llm_tokens"}

ZERO_DECIMAL = {"BIF", "CLP", "DJF", "GNF", "JPY", "KMF", "KRW", "MGA", "PYG", "RWF", "UGX", "VND", "VUV", "XAF", "XOF", "XPF"}
THREE_DECIMAL = {"BHD", "JOD", "KWD", "OMR", "TND"}
SYMBOLS = {"USD": "$", "EUR": "€", "GBP": "£", "INR": "₹", "JPY": "¥"}


def money(minor: int, currency: str) -> str:
    """Amounts are in the currency's smallest unit: 4900 USD = $49, 5000 JPY = ¥5,000."""
    cur = currency.upper()
    div = 1 if cur in ZERO_DECIMAL else 1000 if cur in THREE_DECIMAL else 100
    whole, frac = divmod(int(minor), div)
    text = f"{whole:,}" + (f".{frac:0{len(str(div)) - 1}d}" if frac else "")
    return f"{SYMBOLS[cur]}{text}" if cur in SYMBOLS else f"{cur} {text}"


@dataclass(frozen=True)
class Plan:
    name: str
    monthly_quota: dict  # event -> included units per calendar month (UTC); None = unlimited
    rate_per_minute: int  # requests per minute per tenant
    shared_tools: bool = False  # may use the server's kubectl/GitHub/AWS credentials (single-org deployments)
    cost_checks: bool = False  # may call /cost/check (and Slack `cost ...`): one operator-paid Cost Explorer call each
    hard_limit: bool = False  # True: 429 at the quota. False: usage past the quota is billed as overage
    base_cents: int | None = None  # flat monthly price (minor units); 0 = free; None = not sold online
    currency: str = "usd"
    overage: dict = field(default_factory=dict)  # event -> minor units per unit past the quota (decimal string)
    spend_cap_cents: int | None = None  # max monthly overage charge (minor units); None = uncapped

    def overage_cents(self, used: dict) -> Decimal:
        """Overage charge, in minor units, for this month's usage (event -> units)."""
        return sum((max(0, used.get(e, 0) - inc) * rate for e, (inc, rate) in self.billed_events().items()),
                   Decimal(0))

    @property
    def price_label(self) -> str:
        """What /pricing shows. Derived from the same numbers Stripe is provisioned with."""
        if self.base_cents is None:
            return ""
        label = money(self.base_cents, self.currency) + (" / month" if self.base_cents else "")
        return label + (" + usage" if self.billed_events() else "")

    def billed_events(self) -> dict:
        """event -> (included units, rate) for every event that is metered to Stripe on this plan."""
        return {e: (self.monthly_quota.get(e), Decimal(str(r))) for e, r in self.overage.items()
                if e in EVENTS and self.monthly_quota.get(e) is not None and not self.hard_limit}


# PLACEHOLDER PRICES AND SPEND CAPS: set the real ones here, then run `make stripe-sync`.
PLANS = {
    "free": Plan("free", {"analyze": 50, "cost_check": 0, "llm_tokens": 100_000}, 10, hard_limit=True, base_cents=0),
    "starter": Plan("starter", {"analyze": 1_000, "cost_check": 100, "llm_tokens": 2_000_000}, 60, cost_checks=True,
                    base_cents=4900, spend_cap_cents=10_000,
                    overage={"analyze": "5", "cost_check": "5", "llm_tokens": "0.0002"}),
    "pro": Plan("pro", {"analyze": 10_000, "cost_check": 1_000, "llm_tokens": 20_000_000}, 300, cost_checks=True,
                base_cents=9900, spend_cap_cents=50_000,
                overage={"analyze": "3", "cost_check": "3", "llm_tokens": "0.00015"}),
    "enterprise": Plan("enterprise", {"analyze": None, "cost_check": None, "llm_tokens": None}, 1_000,
                       cost_checks=True),
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
                              cost_checks=cfg.get("cost_checks", base.cost_checks),
                              hard_limit=cfg.get("hard_limit", base.hard_limit),
                              base_cents=cfg.get("base_cents", base.base_cents),
                              spend_cap_cents=cfg.get("spend_cap_cents", base.spend_cap_cents),
                              currency=str(cfg.get("currency", base.currency)),
                              overage={**base.overage, **{k: str(v) for k, v in cfg.get("overage", {}).items()}})
    return plans


def get_plan(name: str) -> Plan:
    plans = _load()
    if name not in plans:
        raise KeyError(f"unknown plan {name!r}")
    return plans[name]
