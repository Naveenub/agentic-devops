import pytest
from fastapi.testclient import TestClient

from api.main import app
from product import billing, store


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("PRODUCT_DB", str(tmp_path / "p.db"))
    monkeypatch.setenv("PRODUCT_ADMIN_KEY", "adm")
    monkeypatch.setenv("AGENT_LLM", "off")
    monkeypatch.delenv("AGENT_API_KEY", raising=False)
    return TestClient(app), monkeypatch


def _tenant(c, plan="free", name="acme"):
    r = c.post("/admin/tenants", json={"name": name, "plan": plan}, headers={"x-admin-key": "adm"})
    assert r.status_code == 200, r.text
    return r.json()


def _ask(c, key, **extra):
    return c.post("/analyze", json={"question": "pod OOMKilled exit code 137", **extra}, headers={"x-api-key": key})


def test_core_mode_unchanged_and_admin_hidden(monkeypatch):
    monkeypatch.delenv("PRODUCT_DB", raising=False)
    c = TestClient(app)
    assert c.post("/admin/tenants", json={"name": "x"}, headers={"x-admin-key": "a"}).status_code == 404
    assert c.get("/usage").status_code == 404
    monkeypatch.setenv("AGENT_API_KEY", "k")
    assert c.post("/analyze", json={"question": "q"}).status_code == 401


def test_admin_requires_key_and_fails_closed(env):
    c, mp = env
    assert c.post("/admin/tenants", json={"name": "x"}).status_code == 401
    mp.delenv("PRODUCT_ADMIN_KEY")
    assert c.post("/admin/tenants", json={"name": "x"}, headers={"x-admin-key": ""}).status_code == 503


def test_tenant_key_auth_and_hashed_storage(env):
    c, _ = env
    t = _tenant(c)
    assert _ask(c, t["api_key"]).status_code == 200
    assert _ask(c, "adk_wrong").status_code == 401
    assert c.post("/analyze", json={"question": "q"}).status_code == 401
    with store.conn() as db:  # plaintext key is never stored
        assert t["api_key"] not in str([tuple(r) for r in db.execute("SELECT * FROM api_keys")])


def test_revoke_and_deactivate(env):
    c, _ = env
    t = _tenant(c)
    h = {"x-admin-key": "adm"}
    assert c.delete(f"/admin/tenants/{t['id']}/keys/{t['api_key'][:8]}", headers=h).json()["revoked"] == 1
    assert _ask(c, t["api_key"]).status_code == 401
    k2 = c.post(f"/admin/tenants/{t['id']}/keys", headers=h).json()["api_key"]
    assert _ask(c, k2).status_code == 200
    c.patch(f"/admin/tenants/{t['id']}", json={"active": 0}, headers=h)
    assert _ask(c, k2).status_code == 401


def test_usage_is_metered_per_tenant(env):
    c, _ = env
    a, b = _tenant(c, name="a"), _tenant(c, name="b")
    for _ in range(3):
        _ask(c, a["api_key"])
    ua = c.get("/usage", headers={"x-api-key": a["api_key"]}).json()["usage"]
    ub = c.get("/usage", headers={"x-api-key": b["api_key"]}).json()["usage"]
    assert ua["analyze"]["used"] == 3 and ub["analyze"]["used"] == 0


def test_llm_tokens_metered(env):
    c, mp = env
    import agent.llm as llm
    mp.delenv("AGENT_LLM")
    mp.setenv("OPENAI_API_KEY", "sk-real")

    class R:
        def raise_for_status(self): pass
        def json(self): return {"usage": {"total_tokens": 321}, "choices": [{"message": {"content": "{}"}}]}

    import httpx
    mp.setattr(httpx, "post", lambda *a, **k: R())
    t = _tenant(c)
    assert _ask(c, t["api_key"], logs="payments-api OOMKilled exit code 137").status_code == 200
    used = c.get("/usage", headers={"x-api-key": t["api_key"]}).json()["usage"]["llm_tokens"]["used"]
    assert used > 0 and used % 321 == 0  # one real LLM call per token report


def test_free_plan_hard_limit_then_paid_overage(env):
    c, mp = env
    mp.setenv("PRODUCT_PLANS", '{"free": {"monthly_quota": {"analyze": 2}}, "starter": {"monthly_quota": {"analyze": 1}}}')
    f = _tenant(c, "free", "f")
    assert [_ask(c, f["api_key"]).status_code for _ in range(3)] == [200, 200, 429]
    s = _tenant(c, "starter", "s")
    assert [_ask(c, s["api_key"]).status_code for _ in range(3)] == [200, 200, 200]  # overage billed, not blocked
    u = c.get("/usage", headers={"x-api-key": s["api_key"]}).json()["usage"]["analyze"]
    assert u == {"used": 3, "included": 1}


def test_rate_limit(env):
    c, mp = env
    mp.setenv("PRODUCT_PLANS", '{"free": {"rate_per_minute": 2}}')
    t = _tenant(c)
    assert [_ask(c, t["api_key"]).status_code for _ in range(3)] == [200, 200, 429]


def test_shared_credential_inputs_blocked_without_plan_grant(env):
    c, mp = env
    t = _tenant(c)
    for extra in ({"kubernetes": {"pod": "p"}}, {"github": {"repo": "o/r", "run_id": "1"}}, {"cost": {"service": "EC2"}}):
        assert _ask(c, t["api_key"], **extra).status_code == 403
    s = _tenant(c, "starter", "s")  # free has no cost_check quota at all; starter has quota but no shared tools
    assert c.post("/cost/check", json={"service": "ec2", "region": "us-east-1"},
                  headers={"x-api-key": s["api_key"]}).status_code == 403
    assert _ask(c, t["api_key"], logs="Error: OOMKilled exit code 137").status_code == 200
    mp.setenv("PRODUCT_PLANS", '{"free": {"shared_tools": true}}')
    assert _ask(c, t["api_key"], cost={"service": "EC2", "region": "r", "daily_costs": [1.0] * 14 + [50.0]}).status_code == 200


def test_unknown_plan_rejected(env):
    c, _ = env
    r = c.post("/admin/tenants", json={"name": "x", "plan": "gold"}, headers={"x-admin-key": "adm"})
    assert r.status_code == 422


def test_stripe_report_is_idempotent_and_only_marks_successes(env):
    c, mp = env
    t = _tenant(c)
    for _ in range(2):
        _ask(c, t["api_key"])
    assert billing.report_usage()["reported"] == 0  # no key configured
    mp.setenv("STRIPE_SECRET_KEY", "sk_test_x")
    mp.setenv("STRIPE_METER_ANALYZE", "agent_analyze_runs")
    assert billing.report_usage(post=lambda d: None)["reported"] == 0  # no customer yet: stays unreported
    c.patch(f"/admin/tenants/{t['id']}", json={"stripe_customer_id": "cus_1"}, headers={"x-admin-key": "adm"})
    calls = []

    class R:
        def __init__(self, code): self.status_code = code

    assert billing.report_usage(post=lambda d: (calls.append(d), R(500))[1]) == {"reported": 0, "failed": 1}
    assert billing.report_usage(post=lambda d: (calls.append(d), R(200))[1]) == {"reported": 1, "failed": 0}
    assert billing.report_usage(post=lambda d: (calls.append(d), R(200))[1])["reported"] == 0  # nothing left
    assert calls[0]["payload[value]"] == "2" and calls[0]["identifier"] == calls[1]["identifier"]


def test_rate_limit_is_shared_across_processes(env):
    """The window lives in the DB, so a second worker (simulated by dropping in-process state) still sees it."""
    c, mp = env
    mp.setenv("PRODUCT_PLANS", '{"free": {"rate_per_minute": 2}}')
    t = _tenant(c)
    assert _ask(c, t["api_key"]).status_code == 200
    import product.metering as m
    assert not hasattr(m, "_hits")
    assert [_ask(c, t["api_key"]).status_code for _ in range(2)] == [200, 429]
    with store.conn() as db:  # rows age out: after the window the tenant may call again
        db.execute("UPDATE rate_hits SET ts = ts - 61")
    assert _ask(c, t["api_key"]).status_code == 200


def _slack(c, mp, text, team="T1", channel="C1"):
    import hashlib, hmac, time
    from urllib.parse import urlencode
    sent = []
    mp.setenv("SLACK_SIGNING_SECRET", "s3")
    import agent.tools.slack as sl
    mp.setattr(sl, "send_message", lambda ch, msg: sent.append(msg))
    body = urlencode({"text": text, "team_id": team, "channel_id": channel}).encode()
    ts = str(int(time.time()))
    sig = "v0=" + hmac.new(b"s3", b"v0:" + ts.encode() + b":" + body, hashlib.sha256).hexdigest()
    r = c.post("/slack/command", content=body, headers={
        "x-slack-request-timestamp": ts, "x-slack-signature": sig,
        "content-type": "application/x-www-form-urlencoded"})
    return r, sent


def test_slack_requires_linked_workspace_and_is_metered(env):
    c, mp = env
    h = {"x-admin-key": "adm"}
    r, sent = _slack(c, mp, "pod OOMKilled exit code 137")
    assert r.json()["response_type"] == "ephemeral" and not sent  # unlinked workspace: refused
    t = _tenant(c)
    assert c.put(f"/admin/tenants/{t['id']}/slack", json={"team_id": "T1"}, headers=h).status_code == 200
    r, sent = _slack(c, mp, "pod OOMKilled exit code 137")
    assert r.json() == {"ok": True} and sent
    r, _ = _slack(c, mp, "pod OOMKilled exit code 137", team="T2")  # a different workspace stays unlinked
    assert r.json()["response_type"] == "ephemeral"
    u = c.get("/usage", headers={"x-api-key": t["api_key"]}).json()["usage"]
    assert u["analyze"]["used"] == 1


def test_slack_enforces_quota_and_shared_tool_grant(env):
    c, mp = env
    h = {"x-admin-key": "adm"}
    mp.setenv("PRODUCT_PLANS", '{"free": {"monthly_quota": {"analyze": 1}}}')
    t = _tenant(c)
    c.put(f"/admin/tenants/{t['id']}/slack", json={"team_id": "T1"}, headers=h)
    assert _slack(c, mp, "q one")[0].json() == {"ok": True}
    r, sent = _slack(c, mp, "q two")
    assert "quota" in r.json()["text"] and not sent
    r, _ = _slack(c, mp, "cost ec2 us-east-1")  # server's AWS creds need the shared_tools grant
    assert r.json()["response_type"] == "ephemeral"
    assert c.delete(f"/admin/tenants/{t['id']}/slack/T1", headers=h).json() == {"unlinked": 1}
    assert "isn't linked" in _slack(c, mp, "q three")[0].json()["text"]


def test_slack_unchanged_in_core_mode(monkeypatch):
    monkeypatch.delenv("PRODUCT_DB", raising=False)
    from api.main import _slack_tenant
    assert _slack_tenant({"text": "x"}) == (None, None)
