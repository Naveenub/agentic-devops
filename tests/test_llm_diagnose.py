import json

import pytest

from agent import llm
from agent.orchestrator import run_agent

OBS = [{"source": "kubectl logs", "content": "FATAL: too many open files\nworker exiting with status 24"}]
OOM = [{"source": "kubectl logs", "content": "Error: Java heap space\nContainer terminated with exit code 137"}]


@pytest.fixture(autouse=True)
def _reset():
    llm.set_client(None)
    yield
    llm.set_client(None)


def _fake(payload, calls=None):
    def fn(system, user):
        if calls is not None:
            calls.append(user)
        return payload if isinstance(payload, str) else json.dumps(payload)
    return fn


def test_llm_diagnoses_unknown_signature_with_verified_evidence():
    llm.set_client(_fake({"diagnosis": "File descriptor limit exhausted.",
                          "evidence": ["FATAL: too many open files"],
                          "suggestions": ["Check ulimit -n: kubectl exec is not needed, inspect the pod spec"]}))
    r = run_agent({"question": "why exiting?", "observations": OBS})
    assert r["category"] == "other_llm" and r["diagnosis_source"] == "llm"
    assert "log: FATAL: too many open files" in r["evidence"]
    assert r["confidence"] == 0.4


def test_fabricated_evidence_is_rejected():
    llm.set_client(_fake({"diagnosis": "Disk full.", "evidence": ["no space left on device"], "suggestions": []}))
    r = run_agent({"question": "why exiting?", "observations": OBS})
    assert r["category"] == "unknown" and r["confidence"] == 0.0


def test_denied_commands_are_filtered():
    llm.set_client(_fake({"diagnosis": "FD limit.", "evidence": ["FATAL: too many open files"],
                          "suggestions": ["kubectl delete pod x", "terraform apply -auto-approve", "Raise nofile limit via pipeline"]}))
    r = run_agent({"question": "q", "observations": OBS})
    assert not any("kubectl delete" in s or "terraform apply" in s for s in r["suggestions"])
    assert r["suggestions"][0] == "Raise nofile limit via pipeline"


def test_signature_category_kept_and_llm_adds_reasoning():
    llm.set_client(_fake({"diagnosis": "JVM heap exhausted; container killed.",
                          "evidence": ["Container terminated with exit code 137"], "suggestions": []}))
    r = run_agent({"question": "payments-api crashing", "observations": OOM})
    assert r["category"] == "oom_killed" and r["diagnosis_source"] == "signature+llm"
    assert r["diagnosis"] == "JVM heap exhausted; container killed."
    assert r["confidence"] == 0.8


def test_cannot_determine_and_garbage_fall_back():
    for payload in ({"cannot_determine": True}, "not json", {"diagnosis": "x", "evidence": []}):
        llm.set_client(_fake(payload))
        assert run_agent({"question": "q", "observations": OBS})["category"] == "unknown"


def test_mock_observations_never_reach_the_llm():
    calls = []
    llm.set_client(_fake({"diagnosis": "x", "evidence": ["mock"], "suggestions": []}, calls))
    mock = [{"source": "github actions", "content": "[MOCK DATA] secret-token", "mock": True}]
    r = run_agent({"question": "q", "observations": mock})
    assert calls == [] and r["category"] == "unknown"


def test_disabled_without_key(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_BASE_URL", raising=False)
    assert not llm.enabled()
    assert run_agent({"question": "q", "observations": OOM})["diagnosis_source"] == "signature"
