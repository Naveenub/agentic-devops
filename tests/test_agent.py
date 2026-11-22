import copy
import subprocess

import pytest

from agent.memory.ingest import infer_type
from agent.nodes.diagnose import diagnose
from agent.nodes.observe import observe
from agent.nodes.suggest import suggest
from agent.orchestrator import run_agent
from agent.state import AgentState
from agent.tools import kubernetes
from agent.tools.github import MOCK_LABEL, get_ci_logs

OOM_OBS = [{"source": "kubectl logs", "content": "Container terminated with exit code 137"}]


def test_nodes_return_partial_updates_and_do_not_mutate_input():
    state = {"question": "q", "observations": copy.deepcopy(OOM_OBS)}
    for node in (observe, diagnose, suggest):
        before = copy.deepcopy(state)
        update = node(state)
        assert isinstance(update, dict)
        assert set(update) <= set(AgentState.__annotations__)
        assert state == before
        state.update(update)


def test_graph_matches_orchestrator():
    from agent.graph import build_graph

    init = {"question": "q", "observations": copy.deepcopy(OOM_OBS)}
    assert build_graph().invoke(copy.deepcopy(init)) == run_agent(init)


def test_known_failure_is_cited_and_confident():
    r = run_agent({"question": "q", "observations": OOM_OBS})
    assert r["category"] == "oom_killed"
    assert r["confidence"] >= 0.6
    assert any(e.startswith("retrieved: ") for e in r["evidence"])


def test_logs_beat_the_question():
    obs = [{"source": "ci", "content": "AccessDeniedException: not authorized to perform s3:PutObject"}]
    assert run_agent({"question": "is this an OOM kill?", "observations": obs})["category"] == "iam_denied"


def test_no_evidence_means_zero_confidence():
    r = run_agent({"question": "why slow?", "observations": [{"source": "app", "content": "all fine"}]})
    assert (r["category"], r["confidence"], r["evidence"]) == ("unknown", 0.0, [])


def test_ci_mock_is_labelled_and_never_evidence():
    assert get_ci_logs("o/r", "1")["mock"] is True
    assert MOCK_LABEL in get_ci_logs("o/r", "1")["content"]
    r = run_agent({"question": "ci failed", "github": {"repo": "o/r", "run_id": "1"}})
    assert r["observations"][0]["mock"] is True
    assert r["category"] == "unknown" and r["confidence"] == 0.0


def test_kubectl_failure_becomes_error_observation_not_evidence(monkeypatch):
    def boom(*a, **k):
        raise FileNotFoundError

    monkeypatch.setattr(subprocess, "run", boom)
    r = run_agent({"question": "q", "kubernetes": {"namespace": "default", "pod": "p"}})
    assert r["observations"][0]["error"] is True
    assert r["category"] == "unknown"


def test_kubectl_argv_is_fixed_and_read_only(monkeypatch):
    seen = {}

    def fake(argv, **kw):
        seen["argv"] = argv
        return subprocess.CompletedProcess(argv, 0, stdout="ok", stderr="")

    monkeypatch.setattr(subprocess, "run", fake)
    assert kubernetes.get_pod_logs("prod", "payments-api-0") == "ok"
    assert seen["argv"][:2] == ["kubectl", "logs"]


@pytest.mark.parametrize("pod", ["--all-namespaces", "a b", "x;rm -rf /", "", None])
def test_kubectl_rejects_unsafe_names(pod):
    with pytest.raises(kubernetes.KubernetesToolError):
        kubernetes.get_pod_logs("default", pod)


def test_synthetic_and_eval_data_stay_out_of_knowledge_base():
    assert infer_type("data/synthetic/incidents.json") == "synthetic"
    assert infer_type("data/eval/golden_incidents.json") == "eval"
    assert infer_type("data/incidents/x.json") == "incident"
