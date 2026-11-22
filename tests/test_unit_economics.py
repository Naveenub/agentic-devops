"""pricing_evidence.json gates the placeholder prices: nothing is assumed about cost or willingness to pay."""
import json

from product import unit_economics as ue

GOOD = {
    "LLM_USD_PER_MILLION_TOKENS": 0.5, "tokens_per_analyze": {"p95": 1500, "mean": 900, "n": 20},
    "COST_CHECK_USD": 0.01, "OTHER_USD_PER_ANALYZE": 0.001, "FIXED_USD_PER_MONTH_PER_TENANT": 2,
    "PAYMENT_FEE_PERCENT": 2.9, "PAYMENT_FEE_FIXED_USD": 0.30, "MIN_GROSS_MARGIN": 0.5,
    "willingness_to_pay": [{"date": "2026-11-01", "source": "call with X", "finding": "would pay $50", "kind": "interview"}],
    "PRICES_CONFIRMED": True,
}


def test_shipped_file_is_not_ready():
    assert ue.problems(json.loads(ue.FILE.read_text())) != []


def test_complete_evidence_passes():
    assert ue.problems(GOOD) == []


def test_margin_math_for_starter():
    m = ue.plan_margins(ue.PLANS["starter"], GOOD)
    # quota cost: 1000*0.001 + 100*0.01 + 2M tokens*$0.5/M + $2 fixed = 1 + 1 + 1 + 2 = $5
    assert float(m["cost_at_quota_usd"]) == 5.0
    fee = 49 * 0.029 + 0.30
    assert abs(float(m["margin_at_quota"]) - (49 - 5 - fee) / 49) < 1e-9
    assert 0 < m["margin_at_cap"] < m["margin_at_quota"]


def test_expensive_llm_breaks_the_margin_floor():
    bad = {**GOOD, "LLM_USD_PER_MILLION_TOKENS": 30.0}
    assert any("below MIN_GROSS_MARGIN" in p for p in ue.problems(bad))


def test_unmet_evidence_and_confirmation_are_reported():
    assert any("willingness_to_pay" in p for p in ue.problems({**GOOD, "willingness_to_pay": [{"source": "x"}]}))
    assert any("PRICES_CONFIRMED" in p for p in ue.problems({**GOOD, "PRICES_CONFIRMED": False}))


def test_token_quota_binding_is_flagged():
    heavy = {**GOOD, "tokens_per_analyze": {"p95": 5000, "mean": 3000, "n": 20}}
    assert any("token quota is the binding limit" in p for p in ue.problems(heavy))


def test_measure_records_p95_from_the_usage_sink(monkeypatch):
    from agent import eval as agent_eval, llm

    monkeypatch.setattr(llm, "enabled", lambda: True)
    counts = iter([0, 1000, 2000, 3000])

    def fake(case, live=False):
        sink = llm.usage_sink.get()
        sink.append(next(counts))
        return {}

    monkeypatch.setattr(agent_eval, "run_case_agent", fake)
    r = ue.measure([{}, {}, {}, {}])
    assert r["n"] == 3 and r["cases_run"] == 4 and r["p95"] == 3000 and r["mean"] == 2000


def test_pro_spend_cap_is_the_thin_spot_at_one_dollar_per_million_tokens():
    thin = {**GOOD, "LLM_USD_PER_MILLION_TOKENS": 1.0}
    assert ue.problems(thin) == ["plan pro: gross margin 34% with spend cap fully used is below MIN_GROSS_MARGIN 50%"]
