import json

import pytest

from agent import eval as ev
from agent import groundedness, hallucination_eval, llm
from agent.nodes.diagnose import _DENY

OOM_CTX = "Container terminated with exit code 137 OOMKilled. Increased memory to 512Mi. Raise the memory limit."


@pytest.fixture(autouse=True)
def _reset():
    llm.set_client(None)
    yield
    llm.set_client(None)


# --- groundedness -----------------------------------------------------------------------------

def test_fabricated_specific_is_flagged_even_when_the_old_metric_passes():
    answer = "Container OOMKilled: memory limit too low. Raise the limit to 2048Mi."
    assert groundedness.lexical_ratio(answer, OOM_CTX) < 0.5          # old metric: looks fine
    r = groundedness.check(answer, OOM_CTX)
    assert r["unsupported_specifics"] == ["2048mi"]


def test_supported_specifics_and_unit_spacing_pass():
    r = groundedness.check("Raise the memory limit to 512 Mi after the exit code 137.", OOM_CTX)
    assert r["unsupported_specifics"] == [] and r["score"] < 0.5


def test_fabricated_arn_and_identifier_are_flagged():
    ctx = "AccessDenied: not authorized to perform iam:PassRole on arn:aws:iam::123456789012:role/ci"
    bad = groundedness.check("Missing iam:CreateRole on arn:aws:iam::999999999999:role/ci", ctx)
    assert "iam:createrole" in bad["unsupported_specifics"]
    assert "arn:aws:iam::999999999999:role/ci" in bad["unsupported_specifics"]
    ok = groundedness.check("Role lacks iam:PassRole", ctx)
    assert ok["unsupported_specifics"] == []


def test_prose_slash_is_not_a_specific():
    assert groundedness.check("credentials/security token expired", "the security token credentials expired")[
        "unsupported_specifics"] == []


def test_stemming_matches_inflections():
    assert groundedness.check("killed", "the pod was OOMKill kills")["score"] == 0.0


def test_judge_flags_unsupported_claims_and_falls_back_on_garbage():
    claims_seen = []

    def fake(system, user):
        claims_seen.append(user)
        return json.dumps({"supported": [True, False]})

    llm.set_client(fake)
    r = groundedness.check("Memory limit too low. The node was drained.", OOM_CTX, judge=True)
    assert r["judge_unsupported_claims"] == ["The node was drained."]

    llm.set_client(lambda s, u: json.dumps({"supported": [True]}))  # wrong length -> ignored
    assert groundedness.check("A. B.", OOM_CTX, judge=True)["judge_unsupported_claims"] is None
    llm.set_client(lambda s, u: "not json")
    assert groundedness.check("A. B.", OOM_CTX, judge=True)["judge_unsupported_claims"] is None


def test_judge_is_off_without_llm(monkeypatch):
    monkeypatch.setenv("AGENT_LLM", "off")
    assert groundedness.check("A. B.", OOM_CTX, judge=True)["judge_unsupported_claims"] is None


def test_hallucination_eval_fails_a_fabricated_llm_diagnosis(monkeypatch):
    case = {"id": "X", "tier": "golden", "query": "why is the worker exiting?",
            "observations": [{"source": "k", "content": "FATAL: too many open files\nworker exiting with status 24"}],
            "expected_category": "other_llm",
            "llm_fixture": {"diagnosis": "Too many open files; raise nofile to 65536.",
                            "evidence": ["FATAL: too many open files"], "suggestions": []}}
    monkeypatch.setattr(hallucination_eval, "load_cases", lambda: [case])
    assert hallucination_eval.main() == 1


# --- eval harness -----------------------------------------------------------------------------

def test_golden_set_covers_every_category_and_guardrail_cases():
    cases = ev.load_cases(include_real=False)
    cats = {c["expected_category"] for c in cases}
    assert {"oom_killed", "iam_denied", "credentials_expired", "image_pull", "other_llm", "unknown"} <= cats
    assert any(c.get("llm_fixture") for c in cases) and any(
        o.get("mock") for c in cases for o in c.get("observations", []))


def test_prompt_injection_suggestion_is_dropped_by_guardrail():
    case = next(c for c in ev.load_cases() if c["id"] == "GOLD-009")
    r = ev.run_case_agent(case)
    assert r["diagnosis_source"] == "signature+llm"
    assert not any(_DENY.search(s) for s in r["suggestions"])
    assert not any("kubectl delete" in s for s in r["suggestions"])


def test_eval_detects_a_wrong_category(tmp_path):
    crit = json.loads((ev.EVAL_DIR / "expected_outputs.json").read_text())
    case = {"id": "T", "tier": "golden", "query": "q", "expected_category": "iam_denied",
            "observations": [{"source": "k", "content": "exit code 137"}]}
    r = ev.evaluate_case(case, crit)
    assert not r["passed"] and r["checks"]["category"] is False


def _write_eval_dir(tmp_path, real):
    d = tmp_path / "eval"
    (d / "real").mkdir(parents=True)
    (d / "golden_incidents.json").write_text(json.dumps(
        [{"id": "G1", "query": "q", "expected_category": "unknown"}]))
    (d / "real" / "a.json").write_text(json.dumps(real))
    return d


def test_real_cases_load_with_tier_and_validate_schema(tmp_path):
    d = _write_eval_dir(tmp_path, [{"id": "R1", "query": "q", "expected_category": "unknown"}])
    assert [(c["id"], c["tier"]) for c in ev.load_cases(d)] == [("G1", "golden"), ("R1", "real")]
    d = _write_eval_dir(tmp_path / "b", [{"id": "R2", "query": "q"}])
    with pytest.raises(ValueError, match="missing"):
        ev.load_cases(d)
    d = _write_eval_dir(tmp_path / "c", [{"id": "G1", "query": "q", "expected_category": "unknown"}])
    with pytest.raises(ValueError, match="duplicate"):
        ev.load_cases(d)


def test_leakage_check_flags_cases_answerable_from_the_knowledge_base():
    leaked = {"id": "R", "query": "q", "expected_category": "oom_killed", "incident_id": "INC-2024-11-EKS-OOM"}
    assert ev.leakage(leaked)
    clean = {"id": "R", "query": "q", "expected_category": "oom_killed",
             "observations": [{"source": "k", "content": "totally novel line that is not in any runbook at all"}]}
    assert ev.leakage(clean) == []


def test_min_real_gate_fails_until_real_cases_exist(capsys):
    assert ev.main(["--min-real", "1"]) == 1
    assert "--min-real" in capsys.readouterr().out
    assert ev.main([]) == 0


def test_live_is_skipped_without_an_llm(monkeypatch, capsys):
    monkeypatch.setenv("AGENT_LLM", "off")
    assert ev.main(["--live"]) == 0
    assert "SKIP --live" in capsys.readouterr().out
