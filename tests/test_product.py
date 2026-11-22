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


# ---- Stripe customer / subscription lifecycle ----

import hashlib as _hl
import hmac as _hm
import json as _json
import time as _t

from product import subscriptions

WH = "whsec_test"


class _R:
    def __init__(self, body, code=200):
        self._b, self.status_code = body, code

    def json(self):
        return self._b


def _sign(body: bytes, ts=None, secret=WH):
    ts = str(int(ts or _t.time()))
    return f"t={ts},v1=" + _hm.new(secret.encode(), ts.encode() + b"." + body, _hl.sha256).hexdigest()


def _hook(c, event, sig=None):
    body = _json.dumps(event).encode()
    return c.post("/stripe/webhook", content=body, headers={"stripe-signature": sig or _sign(body)})


def _sub(tid, cust, status="active", price="price_pro", sid="sub_1", **extra):
    return {"id": f"evt_{_t.time_ns()}", "type": extra.pop("type", "customer.subscription.updated"),
            "data": {"object": {"id": sid, "customer": cust, "status": status, "metadata": {"tenant_id": tid},
                                "items": {"data": [{"price": {"id": price}}]}, **extra}}}


@pytest.fixture
def stripe_env(env):
    c, mp = env
    mp.setenv("STRIPE_SECRET_KEY", "sk_test")
    mp.setenv("STRIPE_WEBHOOK_SECRET", WH)
    mp.setenv("STRIPE_PRICE_STARTER", "price_starter")
    mp.setenv("STRIPE_PRICE_PRO", "price_pro,price_use:metered")
    mp.setenv("STRIPE_SUCCESS_URL", "https://x/ok")
    mp.setenv("STRIPE_CANCEL_URL", "https://x/no")
    calls = []

    def fake(url, data, headers):
        calls.append((url, data, headers))
        if url.endswith("/customers"):
            return _R({"id": "cus_1"})
        return _R({"url": "https://stripe/checkout"})

    return c, mp, calls, fake


def test_tenant_creation_makes_stripe_customer(stripe_env, monkeypatch):
    c, mp, calls, fake = stripe_env
    monkeypatch.setattr(subscriptions.httpx, "post", lambda url, data, headers, auth, timeout: fake(url, data, headers))
    t = _tenant(c)
    assert t["stripe_customer_id"] == "cus_1"
    assert calls[0][1]["metadata[tenant_id]"] == t["id"] and calls[0][2]["Idempotency-Key"]


def test_checkout_creates_session_with_tenant_metadata(stripe_env, monkeypatch):
    c, mp, calls, fake = stripe_env
    monkeypatch.setattr(subscriptions.httpx, "post", lambda url, data, headers, auth, timeout: fake(url, data, headers))
    t = _tenant(c)
    r = c.post("/billing/checkout", json={"plan": "pro"}, headers={"x-api-key": t["api_key"]})
    assert r.json() == {"url": "https://stripe/checkout"}
    d = calls[-1][1]
    assert d["customer"] == "cus_1" and d["client_reference_id"] == t["id"] and d["mode"] == "subscription"
    assert d["line_items[0][quantity]"] == "1" and "line_items[1][quantity]" not in d  # metered price: no quantity
    assert d["subscription_data[metadata][plan]"] == "pro"
    # unpurchasable / unknown plan, missing key
    assert c.post("/billing/checkout", json={"plan": "enterprise"}, headers={"x-api-key": t["api_key"]}).status_code == 422
    assert c.post("/billing/checkout", json={}, headers={"x-api-key": t["api_key"]}).status_code == 422
    assert c.post("/billing/checkout", json={"plan": "pro"}).status_code == 401


def test_checkout_refused_when_already_subscribed(stripe_env, monkeypatch):
    c, mp, calls, fake = stripe_env
    monkeypatch.setattr(subscriptions.httpx, "post", lambda url, data, headers, auth, timeout: fake(url, data, headers))
    t = _tenant(c)
    assert _hook(c, _sub(t["id"], "cus_1")).status_code == 200
    assert c.post("/billing/checkout", json={"plan": "starter"}, headers={"x-api-key": t["api_key"]}).status_code == 409


def test_webhook_rejects_bad_or_stale_signature_and_unconfigured(stripe_env):
    c, mp, *_ = stripe_env
    ev = _sub("ten_x", "cus_1")
    assert _hook(c, ev, sig="t=1,v1=bad").status_code == 400
    body = _json.dumps(ev).encode()
    assert _hook(c, ev, sig=_sign(body, ts=_t.time() - 4000)).status_code == 400
    assert c.post("/stripe/webhook", content=body).status_code == 400
    mp.delenv("STRIPE_WEBHOOK_SECRET")
    assert _hook(c, ev).status_code == 503


def test_webhook_hidden_in_core_mode(monkeypatch):
    monkeypatch.delenv("PRODUCT_DB", raising=False)
    assert TestClient(app).post("/stripe/webhook", content=b"{}").status_code == 404


def test_subscription_upgrades_then_cancellation_downgrades(stripe_env):
    c, mp, *_ = stripe_env
    t = _tenant(c)
    store.update_tenant(t["id"], stripe_customer_id="cus_1")
    assert _hook(c, _sub(t["id"], "cus_1")).status_code == 200
    got = store.get_tenant(t["id"])
    assert (got["plan"], got["stripe_subscription_id"], got["subscription_status"]) == ("pro", "sub_1", "active")
    # plan change in the portal: price swaps to starter
    _hook(c, _sub(t["id"], "cus_1", price="price_starter"))
    assert store.get_tenant(t["id"])["plan"] == "starter"
    # cancellation
    r = _hook(c, {"id": "evt_del", "type": "customer.subscription.deleted",
                  "data": {"object": {"id": "sub_1", "customer": "cus_1", "status": "canceled"}}})
    assert r.status_code == 200
    got = store.get_tenant(t["id"])
    assert (got["plan"], got["active"], got["stripe_subscription_id"], got["subscription_status"]) == ("free", 1, None, "canceled")


def test_failed_payment_downgrades_immediately_and_retry_success_restores(stripe_env):
    c, mp, *_ = stripe_env
    t = _tenant(c)
    store.update_tenant(t["id"], stripe_customer_id="cus_1")
    _hook(c, _sub(t["id"], "cus_1"))
    inv = lambda nxt, i: {"id": f"evt_inv{i}", "type": "invoice.payment_failed",
                          "data": {"object": {"customer": "cus_1", "subscription": "sub_1", "next_payment_attempt": nxt}}}
    _hook(c, inv(1900000000, 1))
    got = store.get_tenant(t["id"])
    assert (got["plan"], got["subscription_status"], got["stripe_subscription_id"]) == ("free", "past_due", "sub_1")
    _hook(c, _sub(t["id"], "cus_1"))  # Stripe retry succeeds
    assert store.get_tenant(t["id"])["plan"] == "pro"
    _hook(c, _sub(t["id"], "cus_1", status="past_due"))  # status alone also downgrades
    assert store.get_tenant(t["id"])["plan"] == "free"
    _hook(c, inv(None, 2))  # retries exhausted
    got = store.get_tenant(t["id"])
    assert (got["plan"], got["subscription_status"]) == ("free", "unpaid")
    # a subscription.updated with status unpaid/canceled also downgrades
    _hook(c, _sub(t["id"], "cus_1"))
    assert store.get_tenant(t["id"])["plan"] == "pro"
    _hook(c, _sub(t["id"], "cus_1", status="unpaid"))
    assert store.get_tenant(t["id"])["plan"] == "free"


def test_webhook_is_idempotent_and_stale_old_subscription_cannot_downgrade(stripe_env):
    c, mp, *_ = stripe_env
    t = _tenant(c)
    store.update_tenant(t["id"], stripe_customer_id="cus_1")
    ev = _sub(t["id"], "cus_1")
    assert _hook(c, ev).json()["result"] == "handled"
    store.update_tenant(t["id"], plan="starter")  # manual change between deliveries
    assert _hook(c, ev).json()["result"] == "duplicate"
    assert store.get_tenant(t["id"])["plan"] == "starter"  # replay did not re-apply
    # old subscription cancelled after the tenant moved to sub_2
    _hook(c, _sub(t["id"], "cus_1", sid="sub_2", price="price_starter"))
    _hook(c, {"id": "evt_old", "type": "customer.subscription.deleted",
              "data": {"object": {"id": "sub_1", "customer": "cus_1", "status": "canceled"}}})
    got = store.get_tenant(t["id"])
    assert (got["plan"], got["stripe_subscription_id"]) == ("starter", "sub_2")


def test_checkout_completed_links_subscription(stripe_env, monkeypatch):
    c, mp, *_ = stripe_env
    t = _tenant(c)
    monkeypatch.setattr(subscriptions, "_fetch_subscription", lambda sid: _sub(t["id"], "cus_9", sid=sid)["data"]["object"])
    r = _hook(c, {"id": "evt_co", "type": "checkout.session.completed",
                  "data": {"object": {"client_reference_id": t["id"], "customer": "cus_9", "subscription": "sub_9"}}})
    assert r.status_code == 200
    got = store.get_tenant(t["id"])
    assert (got["plan"], got["stripe_customer_id"], got["stripe_subscription_id"]) == ("pro", "cus_9", "sub_9")


def test_failed_processing_is_not_recorded_so_stripe_retries(stripe_env, monkeypatch):
    c, mp, *_ = stripe_env
    t = _tenant(c)
    def boom(sid):
        raise subscriptions.StripeError("down")
    monkeypatch.setattr(subscriptions, "_fetch_subscription", boom)
    ev = {"id": "evt_retry", "type": "checkout.session.completed",
          "data": {"object": {"client_reference_id": t["id"], "customer": "cus_1", "subscription": "sub_1"}}}
    assert _hook(c, ev).status_code == 500
    monkeypatch.setattr(subscriptions, "_fetch_subscription", lambda sid: _sub(t["id"], "cus_1")["data"]["object"])
    assert _hook(c, ev).json()["result"] == "handled"


def test_old_database_is_migrated(tmp_path, monkeypatch):
    import sqlite3
    db = tmp_path / "old.db"
    sqlite3.connect(db).executescript("CREATE TABLE tenants (id TEXT PRIMARY KEY, name TEXT NOT NULL, plan TEXT NOT NULL "
        "DEFAULT 'free', stripe_customer_id TEXT, active INTEGER NOT NULL DEFAULT 1, created_at REAL NOT NULL);"
        "INSERT INTO tenants VALUES('ten_1','a','pro','cus_1',1,0);")
    monkeypatch.setenv("PRODUCT_DB", str(db))
    assert store.get_tenant("ten_1")["subscription_status"] is None
