import pytest
from fastapi.testclient import TestClient

from agent.orchestrator import run_agent

STEADY = [100.0, 101, 99, 100, 102, 98, 100, 101]
SPIKE = STEADY[:-1] + [400.0]


def _run(costs):
    return run_agent({"question": "q", "cost": {"service": "EC2", "region": "us-east-1", "daily_costs": costs}})


def test_agent_diagnoses_a_cost_spike():
    r = _run(SPIKE)
    assert r["cost_anomaly"]["anomaly"] is True
    assert r["category"] == "cost_anomaly" and r["confidence"] > 0
    assert any(e.startswith("aws cost explorer: ") for e in r["evidence"])
    assert "Cost Explorer" in r["suggestions"][0]


def test_agent_abstains_on_steady_spend_without_failure_text():
    r = _run(STEADY)
    assert r["cost_anomaly"]["anomaly"] is False
    assert r["category"] == "unknown" and r["confidence"] == 0.0
    assert "within threshold" in r["diagnosis"]


def test_no_data_is_a_flagged_error_never_evidence():
    r = _run([1.0, 2.0])
    assert r["cost_anomaly"]["anomaly"] is None
    assert r["observations"][-1]["error"] is True
    assert r["category"] == "unknown" and r["confidence"] == 0.0


@pytest.fixture
def api(monkeypatch):
    from api.main import app
    import agent.tools.slack as slack

    sent = []
    monkeypatch.setattr(slack, "send_message", lambda ch, text: sent.append((ch, text)))
    monkeypatch.setenv("SLACK_ALERT_CHANNEL", "C123")
    return TestClient(app), sent, monkeypatch


def _stub_costs(monkeypatch, values):
    monkeypatch.setattr("agent.tools.aws_costs.fetch_daily_costs", lambda *a, **k: values)


def test_cost_check_alerts_slack_on_spike(api):
    client, sent, mp = api
    _stub_costs(mp, SPIKE)
    body = client.post("/cost/check", json={"service": "EC2", "region": "us-east-1"}).json()
    assert body["alerted"] is True and body["cost_anomaly"]["anomaly"] is True
    assert len(sent) == 1 and sent[0][0] == "C123" and "Cost anomaly" in sent[0][1]


def test_cost_check_is_quiet_when_steady_or_unconfigured(api):
    client, sent, mp = api
    _stub_costs(mp, STEADY)
    assert client.post("/cost/check", json={"service": "EC2", "region": "r"}).json()["alerted"] is False
    _stub_costs(mp, SPIKE)
    mp.delenv("SLACK_ALERT_CHANNEL")
    assert client.post("/cost/check", json={"service": "EC2", "region": "r"}).json()["alerted"] is False
    assert sent == []


def test_cost_check_ignores_request_channel_and_validates_input(api):
    client, sent, mp = api
    _stub_costs(mp, SPIKE)
    client.post("/cost/check", json={"service": "EC2", "region": "r", "channel": "CATTACKER"})
    assert [c for c, _ in sent] == ["C123"]
    assert client.post("/cost/check", json={"service": "EC2"}).status_code == 422
    assert client.post("/cost/check", json={"service": "EC2", "region": "r", "threshold": "x"}).status_code == 422


def test_analyze_accepts_cost_input(api):
    client, _, mp = api
    _stub_costs(mp, SPIKE)
    r = client.post("/analyze", json={"question": "why is spend up?", "cost": {"service": "EC2", "region": "r"}}).json()
    assert "z=" in r["diagnosis"] and r["confidence"] > 0
