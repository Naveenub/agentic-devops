import os
import subprocess
import sys
import textwrap

import pytest

from agent import eval as golden_eval
from agent import hallucination_eval
from agent.tools import aws_costs
from agent.tools.terraform import parse_error


def test_terraform_parse_error_iam():
    log = open("data/logs/github-actions/terraform-failure.log", encoding="utf-8").read()
    r = parse_error(log)
    assert r["kind"] == "iam_denied"
    assert r["action"] == "iam:PassRole"
    assert r["resource"].startswith("arn:aws:iam::")


def test_terraform_parse_error_other_and_none():
    assert parse_error("Error: Error acquiring the state lock")["kind"] == "state_lock"
    assert parse_error("Error: something odd")["kind"] == "other"
    assert parse_error("all good") is None
    assert parse_error("") is None


def test_zscore_flags_spike_and_not_steady_spend():
    steady = [100.0, 101, 99, 100, 102, 98, 100, 101]
    assert aws_costs.analyze_cost_anomaly("EC2", "us-east-1", daily_costs=steady)["anomaly"] is False
    spike = steady[:-1] + [400.0]
    r = aws_costs.analyze_cost_anomaly("EC2", "us-east-1", daily_costs=spike)
    assert r["anomaly"] is True and r["z_score"] > 2


def test_cost_tool_gives_no_verdict_without_data():
    assert aws_costs.analyze_cost_anomaly("EC2", "r", daily_costs=[1.0, 2.0])["anomaly"] is None


def test_cost_explorer_call_is_read_only_and_parsed():
    calls = []

    class Stub:
        def get_cost_and_usage(self, **kw):
            calls.append(kw)
            return {"ResultsByTime": [{"Total": {"UnblendedCost": {"Amount": str(v)}}} for v in range(10)]}

    r = aws_costs.analyze_cost_anomaly("EC2", "us-east-1", client=Stub())
    assert len(calls) == 1 and r["n_points"] == 10
    assert set(calls[0]) == {"TimePeriod", "Granularity", "Metrics", "Filter"}


def test_cost_explorer_failure_is_an_error_not_a_guess():
    class Boom:
        def get_cost_and_usage(self, **kw):
            raise RuntimeError("denied")

    r = aws_costs.analyze_cost_anomaly("EC2", "r", client=Boom())
    assert r["anomaly"] is None and "denied" in r["error"]


def test_golden_eval_passes():
    assert golden_eval.main() == 0
    assert hallucination_eval.main() == 0


def test_hallucination_score_separates_grounded_from_fabricated():
    q = "exit code 137 OOMKilled memory limit"
    grounded = hallucination_eval.hallucination_score("Container OOMKilled: memory limit too low", q)
    fabricated = hallucination_eval.hallucination_score("Replica lagged from a network partition in us-west", q)
    assert grounded < 0.5 < fabricated


def test_chroma_backend_end_to_end(tmp_path):
    pytest.importorskip("chromadb")
    script = textwrap.dedent("""
        from agent.memory.ingest import ingest_directory
        from agent.memory.retrieval import retrieve
        from agent.orchestrator import run_agent
        ingest_directory()
        ingest_directory()  # idempotent (upsert)
        top = [h.source for h in retrieve("exit code 137 OOMKilled", 2)]
        assert "data/runbooks/eks-oomkill.md" in top, top
        r = run_agent({"question": "q", "observations": [{"source": "k", "content": "exit code 137"}]})
        assert r["category"] == "oom_killed" and r["confidence"] >= 0.6, r
        print("ok")
    """)
    env = {**os.environ, "AGENT_RETRIEVER": "chroma", "CHROMA_EMBEDDER": "hash",
           "CHROMA_PATH": str(tmp_path / "chroma"), "PYTHONPATH": "."}
    p = subprocess.run([sys.executable, "-c", script], env=env, capture_output=True, text=True)
    assert p.returncode == 0 and p.stdout.strip().endswith("ok"), p.stderr[-800:]
