import hashlib
import hmac
import json
import time

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
    monkeypatch.setenv("SLACK_SIGNING_SECRET", "s3cret")
    monkeypatch.setenv("AGENT_API_KEY", "k3y")
    return TestClient(app, headers={"x-api-key": "k3y"}), sent, monkeypatch


def _slack_post(client, body, secret="s3cret", ts=None, sig=None):
    raw = json.dumps(body).encode()
    ts = str(int(time.time()) if ts is None else ts)
    sig = sig or "v0=" + hmac.new(secret.encode(), f"v0:{ts}:".encode() + raw, hashlib.sha256).hexdigest()
    return client.post("/slack/command", content=raw, headers={
        "content-type": "application/json", "x-slack-request-timestamp": ts, "x-slack-signature": sig})


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


# ---- PagerDuty, Slack command, scheduler, Cost Explorer request shape ----

import json  # noqa: E402
import types  # noqa: E402

from agent import cost_watch  # noqa: E402


@pytest.fixture
def pd(monkeypatch):
    calls = []

    def fake_urlopen(req, timeout=None):
        calls.append((req.full_url, json.loads(req.data)))
        return types.SimpleNamespace(close=lambda: None)

    monkeypatch.setattr("agent.cost_watch.urllib.request.urlopen", fake_urlopen)
    return calls


def test_pagerduty_trigger_is_deduped_and_valid_json_even_for_infinite_z(api, pd):
    client, _, mp = api
    mp.setenv("PAGERDUTY_ROUTING_KEY", "rk")
    _stub_costs(mp, [100.0] * 8 + [400.0])  # zero stdev -> z = inf
    body = client.post("/cost/check", json={"service": "EC2", "region": "r"}).json()
    url, sent = pd[0]
    assert body["paged"] is True and url == cost_watch.PD_URL
    assert sent["event_action"] == "trigger" and sent["routing_key"] == "rk"
    assert sent["dedup_key"].endswith(":r") and sent["payload"]["custom_details"]["z_score"] == "inf"


def test_pagerduty_failure_does_not_block_slack_alert(api, monkeypatch):
    client, sent, mp = api
    mp.setenv("PAGERDUTY_ROUTING_KEY", "rk")
    mp.setattr("agent.cost_watch.urllib.request.urlopen", lambda *a, **k: (_ for _ in ()).throw(OSError("down")))
    _stub_costs(mp, SPIKE)
    body = client.post("/cost/check", json={"service": "EC2", "region": "r"}).json()
    assert body["alerted"] is True and body["paged"] is False and "down" in body["page_error"]


def test_no_pagerduty_call_without_key_or_without_spike(api, pd):
    client, _, mp = api
    _stub_costs(mp, SPIKE)
    client.post("/cost/check", json={"service": "EC2", "region": "r"})
    mp.setenv("PAGERDUTY_ROUTING_KEY", "rk")
    _stub_costs(mp, STEADY)
    client.post("/cost/check", json={"service": "EC2", "region": "r"})
    assert pd == []


def test_short_service_names_resolve_to_cost_explorer_names(api):
    client, _, mp = api
    seen = []
    mp.setattr("agent.tools.aws_costs.fetch_daily_costs", lambda s, r, *a, **k: seen.append(s) or STEADY)
    client.post("/cost/check", json={"service": "EC2", "region": "r"})
    assert seen == ["Amazon Elastic Compute Cloud - Compute"]


def test_slack_command_cost_replies_without_alerting(api):
    client, sent, mp = api
    _stub_costs(mp, SPIKE)
    _slack_post(client, {"text": "cost ec2 us-east-1", "channel_id": "C9"})
    assert [c for c, _ in sent] == ["C9"] and "z=" in sent[0][1] and "Suggested" in sent[0][1]
    _slack_post(client, {"text": "cost ec2", "channel_id": "C9"})
    assert "Usage" in sent[1][1]


def test_slack_command_still_diagnoses_other_text(api):
    client, sent, _ = api
    _slack_post(client, {"text": "pod exit code 137", "channel_id": "C9"})
    assert "*Diagnosis:*" in sent[0][1]


def test_api_key_required_on_analyze_and_cost_check(api):
    client, sent, mp = api
    bare = TestClient(client.app)
    for path, body in (("/analyze", {"question": "q"}), ("/cost/check", {"service": "EC2", "region": "r"})):
        assert bare.post(path, json=body).status_code == 401
        assert bare.post(path, json=body, headers={"x-api-key": "wrong"}).status_code == 401
    mp.delenv("AGENT_API_KEY")
    assert client.post("/analyze", json={"question": "q"}).status_code == 503  # fails closed
    assert sent == []


def test_slack_command_acks_then_reports_failure_to_channel(api):
    client, sent, mp = api

    def boom(*a, **k):
        raise RuntimeError("aws down")
    mp.setattr("agent.tools.aws_costs.fetch_daily_costs", boom)
    r = _slack_post(client, {"text": "cost ec2 us-east-1", "channel_id": "C9"})
    assert r.status_code == 200 and r.json() == {"ok": True}
    assert sent == [("C9", "Command failed; see server logs.")]


def test_slack_command_channel_allowlist(api):
    client, sent, mp = api
    mp.setenv("SLACK_ALLOWED_CHANNELS", "C1, C2")
    r = _slack_post(client, {"text": "pod exit code 137", "channel_id": "C9"})
    assert r.json()["response_type"] == "ephemeral" and sent == []
    _slack_post(client, {"text": "pod exit code 137", "channel_id": "C2"})
    assert [c for c, _ in sent] == ["C2"]


def test_docs_are_off_and_healthz_is_open(api):
    bare = TestClient(api[0].app)
    assert [bare.get(p).status_code for p in ("/docs", "/redoc", "/openapi.json")] == [404, 404, 404]
    assert bare.get("/healthz").json() == {"ok": True}


def test_root_and_favicon_do_not_404(api):
    bare = TestClient(api[0].app)
    assert bare.get("/").status_code == 200
    assert bare.get("/").json()["ok"] is True
    assert bare.get("/favicon.ico").status_code == 204


def test_slack_command_rejects_bad_signature_stale_ts_and_missing_secret(api):
    client, sent, mp = api
    body = {"text": "pod exit code 137", "channel_id": "C9"}
    assert client.post("/slack/command", json=body).status_code == 401  # unsigned
    assert _slack_post(client, body, secret="wrong").status_code == 401
    assert _slack_post(client, body, ts=time.time() - 600).status_code == 401  # replay
    mp.delenv("SLACK_SIGNING_SECRET")
    assert _slack_post(client, body).status_code == 503  # fails closed
    assert sent == []


def test_slack_client_is_not_built_at_import(monkeypatch):
    import importlib
    import agent.tools.slack as slack
    monkeypatch.delenv("SLACK_BOT_TOKEN", raising=False)
    importlib.reload(slack)
    assert slack._client.cache_info().currsize == 0


def test_parse_watch():
    assert cost_watch.parse_watch(" EC2:us-east-1 ; Amazon X - Y:eu-west-1;") == [
        ("EC2", "us-east-1"), ("Amazon X - Y", "eu-west-1")]
    assert cost_watch.parse_watch(None) == []
    with pytest.raises(ValueError):
        cost_watch.parse_watch("EC2")


def test_scheduler_respects_cooldown_and_resets_when_clean(api):
    _, sent, mp = api
    watch, last = [("EC2", "r")], {}
    _stub_costs(mp, SPIKE)
    cost_watch.run_watch_once(watch, last, cooldown=100, now=0)
    cost_watch.run_watch_once(watch, last, cooldown=100, now=50)    # inside cooldown: quiet
    assert len(sent) == 1
    cost_watch.run_watch_once(watch, last, cooldown=100, now=150)   # cooldown over: re-alert
    assert len(sent) == 2
    _stub_costs(mp, STEADY)
    cost_watch.run_watch_once(watch, last, cooldown=100, now=160)   # clean verdict resets
    _stub_costs(mp, SPIKE)
    cost_watch.run_watch_once(watch, last, cooldown=100, now=170)   # new spike alerts at once
    assert len(sent) == 3


def test_scheduler_survives_a_failing_check(api):
    _, sent, mp = api

    def boom(s, r, *a, **k):
        if s == "bad":
            raise RuntimeError("x")
        return SPIKE

    mp.setattr("agent.tools.aws_costs.fetch_daily_costs", boom)
    out = cost_watch.run_watch_once([("bad", "r"), ("EC2", "r")], {}, cooldown=0, now=0)
    assert len(out) == 1 and len(sent) == 1


def test_cost_explorer_request_matches_the_real_api_model(monkeypatch):
    """boto3's Stubber validates our request against the real Cost Explorer service model (no network)."""
    boto3 = pytest.importorskip("boto3")
    from botocore.stub import Stubber

    from agent.tools.aws_costs import fetch_daily_costs

    for k in ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY"):
        monkeypatch.setenv(k, "test")
    ce = boto3.client("ce", region_name="us-east-1")
    day = {"TimePeriod": {"Start": "2026-01-01", "End": "2026-01-02"}, "Estimated": False, "Groups": [],
           "Total": {"UnblendedCost": {"Amount": "1.5", "Unit": "USD"}}}
    with Stubber(ce) as stub:
        stub.add_response("get_cost_and_usage", {"ResultsByTime": [day]})
        assert fetch_daily_costs("AWS Lambda", "us-east-1", client=ce) == [1.5]
        stub.assert_no_pending_responses()
