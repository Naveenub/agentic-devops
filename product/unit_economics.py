"""Do the prices in product/plans.py cover what an analysis costs, and is there any evidence anyone will pay them?

Prices are only as good as two things plans.py cannot know: your cost per analysis and what customers will pay.
Both live in product/pricing_evidence.json (override the path with PRODUCT_PRICING_FILE). Nothing here guesses them.

  python -m product.unit_economics measure   run the golden cases through the real LLM; record tokens per analysis
  python -m product.unit_economics check     worst-case margin per plan + what is still missing (exit 1 if anything is)

Evidence entry (willingness_to_pay): {"date": "2026-11-20", "source": "who/where", "finding": "what they said or did",
"kind": "interview|survey|preorder|competitor"}. Real conversations and paid commitments only.

Cost inputs (all USD):
  LLM_USD_PER_MILLION_TOKENS  blended rate of your provider/model for the tokens the agent meters (input + output)
  tokens_per_analyze          {"p95": int, "mean": int, "n": int, "model": str, "measured_on": str}, written by `measure`
  COST_CHECK_USD              one Cost Explorer call (the README says about $0.01)
  OTHER_USD_PER_ANALYZE       retrieval/infra you pay per run; FIXED_USD_PER_MONTH_PER_TENANT: hosting share per tenant
  PAYMENT_FEE_*               your Stripe rate (2.9% + $0.30 is the standard US card rate; check yours)
  MIN_GROSS_MARGIN            e.g. 0.7 = every plan must keep 70% of revenue even when its quota is fully used

Worst case = the whole included quota used, plus the whole spend cap consumed by whichever overage event is
cheapest to sell relative to its cost. Costs use p95 tokens, and assume every run calls the LLM."""
import json
import os
import sys
from datetime import date
from decimal import Decimal
from pathlib import Path

from product.plans import EVENTS, PLANS

FILE = Path(__file__).parent / "pricing_evidence.json"
REQUIRED = ("LLM_USD_PER_MILLION_TOKENS", "COST_CHECK_USD", "OTHER_USD_PER_ANALYZE", "FIXED_USD_PER_MONTH_PER_TENANT",
            "PAYMENT_FEE_PERCENT", "PAYMENT_FEE_FIXED_USD", "MIN_GROSS_MARGIN")
EVIDENCE_KEYS = ("date", "source", "finding")


def path() -> Path:
    return Path(os.getenv("PRODUCT_PRICING_FILE") or FILE)


def load() -> dict:
    try:
        data = json.loads(path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _num(data: dict, key: str):
    v = data.get(key)
    return None if v is None or isinstance(v, bool) or not isinstance(v, (int, float)) else Decimal(str(v))


def unit_costs(data: dict) -> dict | None:
    """USD cost of one unit of each event, or None while an input is missing."""
    rate, per_run, check = (_num(data, k) for k in ("LLM_USD_PER_MILLION_TOKENS", "OTHER_USD_PER_ANALYZE", "COST_CHECK_USD"))
    if None in (rate, per_run, check):
        return None
    return {"analyze": per_run, "cost_check": check, "llm_tokens": rate / Decimal(1_000_000)}


def plan_margins(plan, data: dict) -> dict | None:
    """Revenue, cost and gross margin for a sold plan: quota fully used, and quota + spend cap fully used."""
    costs = unit_costs(data)
    fee_pct, fee_fixed = _num(data, "PAYMENT_FEE_PERCENT"), _num(data, "PAYMENT_FEE_FIXED_USD")
    fixed = _num(data, "FIXED_USD_PER_MONTH_PER_TENANT")
    if costs is None or None in (fee_pct, fee_fixed, fixed) or not plan.base_cents:
        return None

    def margin(revenue: Decimal, cost: Decimal) -> Decimal:
        fees = revenue * fee_pct / 100 + fee_fixed
        return (revenue - cost - fees) / revenue

    base = Decimal(plan.base_cents) / 100
    included = sum((Decimal(plan.monthly_quota.get(e) or 0) * costs[e] for e in EVENTS), Decimal(0)) + fixed
    out = {"base_usd": base, "cost_at_quota_usd": included, "margin_at_quota": margin(base, included)}
    if plan.spend_cap_cents is not None and plan.billed_events():
        cap = Decimal(plan.spend_cap_cents) / 100
        worst = None
        for e, (_, rate_cents) in plan.billed_events().items():
            units = cap / (rate_cents / 100)
            m = margin(base + cap, included + units * costs[e])
            worst = m if worst is None or m < worst else worst
        out["margin_at_cap"] = worst
    return out


def problems(data: dict | None = None) -> list:
    data = load() if data is None else data
    out = [f"missing: {k}" for k in REQUIRED if _num(data, k) is None]
    tpa = data.get("tokens_per_analyze")
    if not (isinstance(tpa, dict) and isinstance(tpa.get("p95"), int) and tpa["p95"] > 0 and tpa.get("n", 0) > 0):
        out.append("missing: tokens_per_analyze (run: python -m product.unit_economics measure)")
    floor = _num(data, "MIN_GROSS_MARGIN")
    if floor is not None:
        for name, plan in PLANS.items():
            m = plan_margins(plan, data)
            if m is None:
                continue
            for label, key in (("quota fully used", "margin_at_quota"), ("spend cap fully used", "margin_at_cap")):
                if key in m and m[key] < floor:
                    out.append(f"plan {name}: gross margin {m[key]:.0%} with {label} is below MIN_GROSS_MARGIN {floor:.0%}")
            if isinstance(tpa, dict) and isinstance(tpa.get("p95"), int):
                need = (plan.monthly_quota.get("analyze") or 0) * tpa["p95"]
                have = plan.monthly_quota.get("llm_tokens")
                if have is not None and need > have:
                    out.append(f"plan {name}: {plan.monthly_quota['analyze']} analyses x {tpa['p95']} p95 tokens = {need:,} "
                               f"tokens, more than the {have:,} included; the token quota is the binding limit")
    ev = data.get("willingness_to_pay")
    good = [e for e in ev if isinstance(e, dict) and all(str(e.get(k, "")).strip() for k in EVIDENCE_KEYS)] \
        if isinstance(ev, list) else []
    if not good:
        out.append("missing: willingness_to_pay evidence (interviews, preorders, competitor pricing; see the docstring)")
    if data.get("PRICES_CONFIRMED") is not True:
        out.append("missing: PRICES_CONFIRMED (set to true once you have decided the prices in plans.py on this evidence)")
    return out


def measure(cases: list | None = None) -> dict:
    """Run the golden cases against the real LLM and record tokens per analysis. Needs OPENAI_API_KEY/OPENAI_BASE_URL."""
    from agent import eval as agent_eval, llm

    if not llm.enabled():
        raise SystemExit("measure needs a real LLM (OPENAI_API_KEY or OPENAI_BASE_URL); none is configured")
    cases = cases if cases is not None else agent_eval.load_cases()
    per_run = []
    for case in cases:
        tokens: list = []
        reset = llm.usage_sink.set(tokens)
        try:
            agent_eval.run_case_agent(case, live=True)
        finally:
            llm.usage_sink.reset(reset)
        per_run.append(sum(tokens))
    used = sorted(t for t in per_run if t > 0)
    if not used:
        raise SystemExit("no case called the LLM, so there is nothing to measure (are the cases resolved by signatures?)")
    p95 = used[min(len(used) - 1, int(len(used) * 0.95))]
    return {"p95": p95, "mean": round(sum(used) / len(used)), "n": len(used), "cases_run": len(per_run),
            "model": os.getenv("AGENT_LLM_MODEL", "gpt-4o-mini"), "measured_on": date.today().isoformat()}


def main(argv: list) -> int:
    cmd = argv[0] if argv else "check"
    data = load()
    if cmd == "measure":
        result = measure()
        data["tokens_per_analyze"] = result
        path().write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
        print(f"tokens per analysis that reached the LLM: p95 {result['p95']:,}, mean {result['mean']:,} "
              f"({result['n']} of {result['cases_run']} cases, {result['model']}); saved to {path()}")
        print("These are the golden cases, not your customers' traffic: re-measure on real incidents once you have them.")
        return 0
    if cmd != "check":
        print("usage: python -m product.unit_economics [check|measure]")
        return 2
    for name, plan in PLANS.items():
        m = plan_margins(plan, data)
        if m:
            cap = f", {m['margin_at_cap']:.0%} with the spend cap used" if "margin_at_cap" in m else ""
            print(f"{name}: ${m['base_usd']}/month, cost ${m['cost_at_quota_usd']:.2f} at full quota, "
                  f"margin {m['margin_at_quota']:.0%}{cap}")
    gaps = problems(data)
    for g in gaps:
        print(g)
    print("pricing: OK" if not gaps else "pricing: NOT READY (plans.py prices are still placeholders)")
    return 1 if gaps else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
