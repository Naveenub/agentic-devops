import json
import os

import pytest
from fastapi.testclient import TestClient

from api.main import app
from product import billing, legal, mailer, plans as plans_mod, store, subscriptions


@pytest.fixture
def env(tmp_path, monkeypatch):
    pg = os.getenv("TEST_POSTGRES_URL")  # set it to run this whole file against PostgreSQL instead of SQLite
    monkeypatch.setenv("PRODUCT_DB", pg or str(tmp_path / "p.db"))
    if pg:
        with store.conn() as db:
            db.execute("TRUNCATE tenants, stripe_events, api_keys, usage, rate_hits, slack_links, signup_hits, "
                       "recover_hits, recovery_tokens, verify_tokens, job_runs, tenant_members, member_invites, invite_hits, sso_hits, tenant_sso RESTART IDENTITY CASCADE")
    monkeypatch.setenv("PRODUCT_ADMIN_KEY", "adm")
    monkeypatch.setenv("AGENT_LLM", "off")
    monkeypatch.setenv("PRODUCT_SIGNUP", "on")  # signup is closed unless opened; most tests need it open
    legal_file = tmp_path / "entity.json"  # signup also needs the legal documents to be published
    legal_file.write_text(json.dumps({**{k: "x" for k in legal.FIELDS}, "COUNSEL_REVIEWED": True}))
    monkeypatch.setenv("PRODUCT_LEGAL_FILE", str(legal_file))
    monkeypatch.delenv("AGENT_API_KEY", raising=False)
    for k in ("PRODUCT_SMTP_HOST", "PRODUCT_SMTP_FROM", "PRODUCT_PUBLIC_URL", "STRIPE_SECRET_KEY"):
        monkeypatch.delenv(k, raising=False)
    return TestClient(app), monkeypatch


_n = iter(range(10**6))


def _signup(c, name="acme", email=None, **headers):
    """POST /signup with a unique email unless one is given (emails are unique per tenant)."""
    return c.post("/signup", json={"name": name, "email": email or f"user{next(_n)}@example.com", "accept_terms": True},
                  headers=headers)


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
    s = _tenant(c, "starter", "s")  # starter may cost-check but gets no kubectl/GitHub
    assert _ask(c, s["api_key"], kubernetes={"pod": "p"}).status_code == 403
    assert _ask(c, s["api_key"], cost={"service": "EC2"}).status_code == 403
    assert _ask(c, t["api_key"], logs="Error: OOMKilled exit code 137").status_code == 200
    mp.setenv("PRODUCT_PLANS", '{"free": {"shared_tools": true}}')
    assert _ask(c, t["api_key"], cost={"service": "EC2", "region": "r", "daily_costs": [1.0] * 14 + [50.0]}).status_code == 200


def test_cost_check_plan_grants(env):
    c, mp = env
    body = {"service": "ec2", "region": "us-east-1"}
    mp.setattr("api.main.check_cost", lambda *a, **k: {"cost_anomaly": {"anomaly": False, "message": "ok"}})
    for plan, code in (("free", 429), ("starter", 200), ("pro", 200), ("enterprise", 200)):
        t = _tenant(c, plan, plan)
        assert c.post("/cost/check", json=body, headers={"x-api-key": t["api_key"]}).status_code == code
    mp.setenv("PRODUCT_PLANS", '{"free": {"cost_checks": true, "monthly_quota": {"cost_check": 5}}, "pro": {"cost_checks": false}}')
    f, p = _tenant(c, "free", "f2"), _tenant(c, "pro", "p2")
    assert c.post("/cost/check", json=body, headers={"x-api-key": f["api_key"]}).status_code == 200
    assert c.post("/cost/check", json=body, headers={"x-api-key": p["api_key"]}).status_code == 403


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
    mp.setenv("STRIPE_PRICE_STARTER", "price_starter,price_starter_seat:seat")
    mp.setenv("STRIPE_PRICE_PRO", "price_pro,price_use:metered,price_seat:seat")
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
    assert d["line_items[2][price]"] == "price_seat" and d["line_items[2][quantity]"] == "1"  # seat price: one per active member
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


def test_failed_payment_reads_new_api_invoice_shape(stripe_env):
    c, mp, *_ = stripe_env
    t = _tenant(c)
    store.update_tenant(t["id"], stripe_customer_id="cus_1")
    _hook(c, _sub(t["id"], "cus_1"))
    inv = lambda sid, i: {"id": f"evt_new{i}", "type": "invoice.payment_failed",
                          "data": {"object": {"customer": "cus_1", "next_payment_attempt": 1900000000,
                                              "parent": {"subscription_details": {"subscription": sid}}}}}
    _hook(c, inv("sub_other", 1))  # an unrelated subscription's invoice must not touch the tenant
    assert store.get_tenant(t["id"])["plan"] == "pro"
    _hook(c, inv("sub_1", 2))
    got = store.get_tenant(t["id"])
    assert (got["plan"], got["subscription_status"], got["stripe_subscription_id"]) == ("free", "past_due", "sub_1")


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
    assert store.get_tenant("ten_1")["email"] is None  # tenants from before key recovery have none, and may share that
    assert store.create_tenant("b", "free", "b@example.com")["email"] == "b@example.com"
    store.create_tenant("c")  # a second tenant without an email is fine: NULLs never collide
    with store.conn() as db:
        assert {r[1] for r in db.execute("PRAGMA index_list(tenants)")} >= {"tenants_email_uq"}


# ---- self-serve: pricing, signup, account/billing page ------------------------------------------

def test_web_pages_hidden_in_core_mode(monkeypatch):
    monkeypatch.delenv("PRODUCT_DB", raising=False)
    c = TestClient(app)
    for path in ("/pricing", "/signup", "/account", "/billing"):
        assert c.get(path).status_code == 404
    assert c.post("/signup", json={"name": "x"}).status_code == 404


def test_signup_creates_free_tenant_and_working_key(env):
    c, _ = env
    r = _signup(c, name="  Acme   Corp ", email="  Ops@Example.COM ")
    assert r.status_code == 200
    t = r.json()
    assert t["plan"] == "free" and t["name"] == "Acme Corp" and t["api_key"].startswith("adk_")
    assert t["email"] == "ops@example.com"  # stored normalised, so recovery matches however it is typed
    assert _ask(c, t["api_key"]).status_code == 200
    assert c.get("/usage", headers={"x-api-key": t["api_key"]}).json()["plan"] == "free"
    with store.conn() as db:  # plaintext key is never stored; no Stripe customer is made for anonymous signups
        assert t["api_key"] not in str([tuple(x) for x in db.execute("SELECT * FROM api_keys")])
        assert db.execute("SELECT stripe_customer_id FROM tenants WHERE id=?", (t["id"],)).fetchone()[0] is None


def test_signup_ignores_requested_plan_and_validates(env):
    c, _ = env
    ok = {"name": "x", "email": "a@example.com"}
    assert c.post("/signup", json={**ok, "plan": "enterprise", "accept_terms": True}).json()["plan"] == "free"
    assert c.post("/signup", json={"email": "b@example.com", "accept_terms": True}).status_code == 422
    assert c.post("/signup", json={"name": "   ", "email": "b@example.com", "accept_terms": True}).status_code == 422
    assert c.post("/signup", json={"name": 5, "email": "b@example.com", "accept_terms": True}).status_code == 422
    assert c.post("/signup", json={"name": "x" * 101, "email": "b@example.com", "accept_terms": True}).status_code == 422


def test_signup_requires_a_valid_unique_email(env):
    c, _ = env
    for bad in (None, "", "   ", "nope", "a@b", "a b@example.com", "a@@example.com", 5, "x" * 250 + "@example.com",
                "a@example.com\r\nBcc: evil@example.com"):
        r = c.post("/signup", json={"name": "x", "email": bad, "accept_terms": True})
        assert r.status_code == 422, bad
    assert c.post("/signup", json={"name": "x"}).status_code == 422
    assert _signup(c, email="dup@example.com").status_code == 200
    dup = _signup(c, name="other", email="DUP@example.com")  # same address, different case
    assert dup.status_code == 409 and "already exists" in dup.json()["detail"]
    assert "key recovery" not in dup.json()["detail"]  # mail not configured: points at the operator instead
    with store.conn() as db:
        assert db.execute("SELECT COUNT(*) FROM tenants").fetchone()[0] == 1  # the refused signup left nothing behind


def test_signup_rate_limited_per_ip_and_per_day(env):
    c, mp = env
    mp.setenv("PRODUCT_SIGNUP_PER_IP_HOUR", "2")
    assert [_signup(c).status_code for _ in range(3)] == [200, 200, 429]
    mp.setenv("PRODUCT_TRUST_PROXY", "1")  # a different (proxy-reported) client gets its own allowance
    assert _signup(c, **{"x-forwarded-for": "1.2.3.4"}).status_code == 200
    mp.setenv("PRODUCT_SIGNUP_PER_DAY", "3")  # ...but the deployment-wide cap still applies
    assert _signup(c, **{"x-forwarded-for": "5.6.7.8"}).status_code == 429


def test_signup_is_closed_unless_the_operator_opens_it(env):
    c, mp = env
    for value in (None, "", "off", "0", "false", "maybe"):  # anything but an explicit yes stays closed
        mp.delenv("PRODUCT_SIGNUP", raising=False)
        if value is not None:
            mp.setenv("PRODUCT_SIGNUP", value)
        assert _signup(c).status_code == 403, value
        assert "closed" in c.get("/signup").text
        assert "<form" not in c.get("/signup").text
    assert _tenant(c)["plan"] == "free"  # operators can still provision
    for value in ("on", "1", "true", "yes", "OPEN"):
        mp.setenv("PRODUCT_SIGNUP", value)
        assert _signup(c).status_code == 200, value


def test_pricing_page_lists_plans_and_only_offers_what_can_be_bought(stripe_env):
    c, mp, _, _ = stripe_env
    r = c.get("/pricing")
    assert r.status_code == 200 and "text/html" in r.headers["content-type"]
    page = r.text
    assert "50 analyze runs / month" in page and "Unlimited" in page and "$99 / month + $25 / seat + usage" in page
    assert "/signup?plan=starter" in page and "/signup?plan=pro" in page
    assert "/signup?plan=enterprise" not in page and "Not available yet" in page  # no price configured
    mp.setenv("PRODUCT_CONTACT_URL", "mailto:sales@example.com")
    assert 'href="mailto:sales@example.com"' in c.get("/pricing").text
    mp.delenv("STRIPE_SECRET_KEY")  # Stripe off: nothing paid is offered
    assert "/signup?plan=" not in c.get("/pricing").text


def test_pricing_page_escapes_configured_text(env):
    c, mp = env
    mp.setenv("PRODUCT_PLANS", '{"free": {"currency": "<script>alert(1)</script>"}}')
    mp.setenv("PRODUCT_CONTACT_URL", '"><script>x</script>')
    page = c.get("/pricing").text
    assert "<script>alert(1)" not in page and "<script>x</script>" not in page


def test_html_pages_are_locked_down(env):
    c, _ = env
    for path in ("/pricing", "/signup", "/account", "/recover"):
        r = c.get(path)
        assert r.status_code == 200
        csp = r.headers["content-security-policy"]
        assert "default-src 'none'" in csp and "'unsafe-inline'" not in csp and "frame-ancestors 'none'" in csp
        assert r.headers["x-frame-options"] == "DENY"
    assert c.get("/account").headers["cache-control"] == "no-store"
    assert c.get("/signup").headers["cache-control"] == "no-store"
    n1, n2 = (c.get("/account").headers["content-security-policy"] for _ in range(2))
    assert n1 != n2  # fresh nonce per response


def test_billing_state_drives_the_account_page(stripe_env, monkeypatch):
    c, mp, calls, fake = stripe_env
    monkeypatch.setattr(subscriptions.httpx, "post", lambda url, data, headers, auth, timeout: fake(url, data, headers))
    assert c.get("/billing").status_code == 401
    t = _signup(c).json()
    h = {"x-api-key": t["api_key"]}
    s = c.get("/billing", headers=h).json()
    assert s["plan"] == "free" and s["purchasable"] == ["starter", "pro"]
    assert s["has_subscription"] is False and s["can_manage"] is False
    assert c.post("/billing/portal", headers=h).status_code == 409  # no customer yet: page hides the button
    # checkout (what the page's Upgrade button calls) creates the customer, then the portal opens
    url = c.post("/billing/checkout", json={"plan": "pro", "success_url": "http://t/a", "cancel_url": "http://t/b"}, headers=h)
    assert url.json() == {"url": "https://stripe/checkout"}
    assert c.get("/billing", headers=h).json()["can_manage"] is True
    assert c.post("/billing/portal", json={"return_url": "http://t/a"}, headers=h).json()["url"] == "https://stripe/checkout"
    # once subscribed, upgrades go through the portal, so no Upgrade buttons
    r = _hook(c, _sub(t["id"], "cus_1", price="price_pro"))
    assert r.status_code == 200
    s = c.get("/billing", headers=h).json()
    assert s["plan"] == "pro" and s["has_subscription"] and s["purchasable"] == [] and s["subscription_status"] == "active"


def test_billing_state_without_stripe_offers_nothing(env):
    c, _ = env
    t = _signup(c).json()
    s = c.get("/billing", headers={"x-api-key": t["api_key"]}).json()
    assert s["purchasable"] == [] and s["can_manage"] is False


# ---- prices on /pricing come from plans.py ------------------------------------------------------

import re as _re

def test_pricing_shows_catalog_prices_without_calling_stripe(stripe_env, monkeypatch):
    c, _, _, _ = stripe_env

    def boom(*a, **k):
        raise AssertionError("/pricing must not call Stripe")

    monkeypatch.setattr(subscriptions.httpx, "get", boom)
    page = c.get("/pricing").text
    assert "$0" in page and "$49 / month + $15 / seat + usage" in page and "$99 / month + $25 / seat + usage" in page
    assert "Shown at checkout" not in page


def test_price_override_goes_through_plans_not_free_text(stripe_env):
    c, mp, _, _ = stripe_env
    mp.setenv("PRODUCT_PLANS", '{"starter": {"base_cents": 3900, "overage": {}}}')
    page = c.get("/pricing").text
    assert "$39 / month + $15 / seat + usage" in page  # overage merges with the default; rates stay in plans.py
    mp.setenv("PRODUCT_PLANS", '{"starter": {"currency": "<script>alert(1)</script>"}}')
    assert "<script>alert(1)" not in c.get("/pricing").text


def test_price_formatting():
    money = plans_mod.money
    assert money(4900, "usd") == "$49" and money(4950, "USD") == "$49.50" and money(5, "usd") == "$0.05"
    assert money(5000, "jpy") == "¥5,000" and money(1234567, "inr") == "₹12,345.67"
    assert money(1500, "chf") == "CHF 15" and money(1500, "kwd") == "KWD 1.500"


def test_every_paid_plan_has_a_price_and_billed_events_are_metered():
    for name, p in plans_mod.PLANS.items():
        if name in ("free", "enterprise"):
            continue
        assert p.base_cents, name  # a paid plan with no price in the repo is the bug this guards against
        assert set(p.overage) <= set(plans_mod.EVENTS) and all(p.monthly_quota[e] is not None for e in p.overage)


def test_pricing_when_signup_is_closed_sends_people_to_their_account(stripe_env):
    c, mp, _, _ = stripe_env
    mp.delenv("PRODUCT_SIGNUP")
    page = c.get("/pricing").text
    assert "Start free" not in page and "/signup?plan=" not in page and "Signup is closed" in page
    assert 'href="/account?upgrade=starter"' in page and 'href="/account?upgrade=pro"' in page
    mp.setenv("PRODUCT_CONTACT_URL", "mailto:sales@example.com")
    assert "Signup is closed" not in c.get("/pricing").text and "Contact us" in c.get("/pricing").text


# ---- email at signup and key recovery -----------------------------------------------------------

@pytest.fixture
def mail_env(env):
    c, mp = env
    mp.setenv("PRODUCT_SMTP_HOST", "smtp.test")
    mp.setenv("PRODUCT_SMTP_FROM", "DevOps <no-reply@example.com>")
    mp.setenv("PRODUCT_PUBLIC_URL", "https://agent.example.com/")
    class Sent(list):  # recovery mail; signup verification mail is kept apart so recovery tests count only theirs
        verification: list = []

    sent = Sent()
    sent.verification = []
    mp.setattr(mailer, "send", lambda to, subject, body: (sent.verification if subject.startswith("Confirm your email")
                                                          else sent).append((to, subject, body)))
    return c, mp, sent


def _token(sent):
    return _re.search(r"https://agent\.example\.com/recover#t=([A-Za-z0-9_-]+)", sent[-1][2]).group(1)


def _confirm(c, tok):
    return c.post("/recover/confirm", json={"token": tok})


def test_lost_key_is_recovered_by_emailed_link_and_old_keys_die(mail_env):
    c, _, sent = mail_env
    t = _signup(c, email="Lost@Example.com").json()
    extra = c.post(f"/admin/tenants/{t['id']}/keys", headers={"x-admin-key": "adm"}).json()["api_key"]
    r = c.post("/recover", json={"email": "lost@example.com"})
    assert r.status_code == 202 and len(sent) == 1 and sent[0][0] == "lost@example.com"
    assert "revokes every existing key" in sent[0][2] and "30 minutes" in sent[0][2]
    tok = _token(sent)
    # an address nobody registered gets the identical answer and no mail, so this cannot be used to probe for accounts
    r2 = c.post("/recover", json={"email": "nobody@example.com"})
    assert r2.status_code == 202 and r2.json() == r.json() and len(sent) == 1
    # merely opening the page (what a mail scanner does) does not spend the link
    assert c.get("/recover").status_code == 200
    got = _confirm(c, tok)
    assert got.status_code == 200 and got.json()["id"] == t["id"] and got.json()["plan"] == "free"
    new = got.json()["api_key"]
    assert new.startswith("adk_") and new not in (t["api_key"], extra)
    assert _ask(c, t["api_key"]).status_code == 401 and _ask(c, extra).status_code == 401  # every old key is revoked
    assert _ask(c, new).status_code == 200
    assert _confirm(c, tok).status_code == 400  # single use
    with store.conn() as db:  # neither the token nor the key is stored in the clear
        stored = str([tuple(x) for x in db.execute("SELECT * FROM recovery_tokens")]) + \
            str([tuple(x) for x in db.execute("SELECT * FROM api_keys")])
        assert tok not in stored and new not in stored


def test_recovery_link_expires_replaces_older_links_and_is_throttled(mail_env):
    c, _, sent = mail_env
    _signup(c, email="a@example.com")
    c.post("/recover", json={"email": "a@example.com"})
    assert len(sent) == 1
    first = _token(sent)
    assert c.post("/recover", json={"email": "a@example.com"}).status_code == 202 and len(sent) == 1  # cooldown: no mailbombing
    with store.conn() as db:
        db.execute("UPDATE recovery_tokens SET expires_at=?", (_t.time() - 1,))
    assert _confirm(c, first).status_code == 400  # expired
    with store.conn() as db:  # past the cooldown, a new request mails a fresh link and retires the old one
        db.execute("UPDATE recovery_tokens SET created_at=created_at-?", (store.RECOVERY_COOLDOWN + 1,))
    c.post("/recover", json={"email": "a@example.com"})
    second = _token(sent)
    assert len(sent) == 2 and second != first
    assert _confirm(c, first).status_code == 400 and _confirm(c, second).status_code == 200


def test_recovery_validation_inactive_tenants_and_junk_tokens(mail_env):
    c, _, sent = mail_env
    t = _signup(c, email="gone@example.com").json()
    c.patch(f"/admin/tenants/{t['id']}", json={"active": 0}, headers={"x-admin-key": "adm"})
    assert c.post("/recover", json={"email": "gone@example.com"}).status_code == 202 and sent == []  # deactivated: no mail
    for bad in ({}, {"email": ""}, {"email": "nope"}, {"email": 5}):
        assert c.post("/recover", json=bad).status_code == 422
    for tok in (None, 5, "", "short", "x" * 300, "A" * 43):
        assert _confirm(c, tok).status_code == 400
    # a token that was issued before the tenant was deactivated no longer works either
    t2 = _signup(c, email="soon@example.com").json()
    c.post("/recover", json={"email": "soon@example.com"})
    c.patch(f"/admin/tenants/{t2['id']}", json={"active": 0}, headers={"x-admin-key": "adm"})
    assert _confirm(c, _token(sent)).status_code == 400


def test_recovery_requests_are_rate_limited_per_ip(mail_env):
    c, mp, _ = mail_env
    mp.setenv("PRODUCT_RECOVER_PER_IP_HOUR", "2")
    assert [c.post("/recover", json={"email": f"x{i}@example.com"}).status_code for i in range(3)] == [202, 202, 429]


def test_recovery_is_off_without_complete_mail_config(env):
    c, mp = env
    assert c.post("/recover", json={"email": "a@example.com"}).status_code == 503
    assert "not set up" in c.get("/recover").text and "<form" not in c.get("/recover").text
    mp.setenv("PRODUCT_SMTP_HOST", "smtp.test")
    mp.setenv("PRODUCT_SMTP_FROM", "no-reply@example.com")  # still no PRODUCT_PUBLIC_URL: links would have to trust the Host header
    assert c.post("/recover", json={"email": "a@example.com"}).status_code == 503
    mp.setenv("PRODUCT_PUBLIC_URL", "agent.example.com")  # not a URL
    assert c.post("/recover", json={"email": "a@example.com"}).status_code == 503
    mp.setenv("PRODUCT_PUBLIC_URL", "https://agent.example.com")
    assert c.post("/recover", json={"email": "a@example.com"}).status_code == 202


def test_recovery_link_ignores_the_host_header(mail_env):
    c, _, sent = mail_env
    _signup(c, email="h@example.com")
    c.post("/recover", json={"email": "h@example.com"}, headers={"host": "evil.example", "x-forwarded-host": "evil.example"})
    assert "https://agent.example.com/recover#t=" in sent[0][2] and "evil.example" not in sent[0][2]


def test_signup_and_recovery_pages_mention_the_right_way_back(mail_env):
    c, mp, _ = mail_env
    signup = c.get("/signup").text
    assert 'name="email"' in signup and 'type="email"' in signup and 'href="/recover"' in signup
    page = c.get("/recover")
    assert 'id="ask"' in page.text and "location.hash" in page.text  # the token travels in the fragment, never to a server
    assert page.headers["cache-control"] == "no-store" and "default-src 'none'" in page.headers["content-security-policy"]
    _signup(c, email="d@example.com")
    assert "key recovery" in _signup(c, email="d@example.com").json()["detail"]  # a duplicate points at recovery
    mp.delenv("PRODUCT_SMTP_HOST")  # without mail, the pages send people to the operator instead of a dead link
    off = c.get("/signup").text
    assert 'href="/recover"' not in off and "Ask the operator" in off
    mp.setenv("PRODUCT_SIGNUP", "off")
    assert 'href="/recover"' not in c.get("/signup").text  # closed signup page offers recovery only when it works


def test_admin_can_find_a_tenant_by_email_and_create_with_one(env):
    c, _ = env
    h = {"x-admin-key": "adm"}
    made = c.post("/admin/tenants", json={"name": "n", "email": " Ops@Example.com "}, headers=h)
    assert made.status_code == 200 and made.json()["email"] == "ops@example.com"
    assert c.post("/admin/tenants", json={"name": "n2"}, headers=h).json()["email"] is None  # optional for operators
    assert c.post("/admin/tenants", json={"name": "n3", "email": "OPS@example.com"}, headers=h).status_code == 409
    assert c.post("/admin/tenants", json={"name": "n4", "email": "bad"}, headers=h).status_code == 422
    found = c.get("/admin/tenants", params={"email": "OPS@example.com"}, headers=h)
    assert found.status_code == 200 and found.json()["id"] == made.json()["id"]
    assert "api_key" not in found.json()
    assert c.get("/admin/tenants", params={"email": "no@example.com"}, headers=h).status_code == 404
    assert c.get("/admin/tenants", params={"email": "bad"}, headers=h).status_code == 422
    assert c.get("/admin/tenants", params={"email": "ops@example.com"}).status_code == 401  # operator-only


class _FakeSMTP:
    log: list = []

    def __init__(self, host, port, timeout=None, context=None):
        self.log.append(("connect", type(self).__name__, host, port, timeout))

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def starttls(self, context=None):
        self.log.append(("starttls", context is not None))

    def login(self, user, password):
        self.log.append(("login", user, password))

    def send_message(self, msg, from_addr=None, to_addrs=None):
        self.log.append(("send", msg["From"], msg["To"], msg["Subject"], msg.get_content().strip(), from_addr, to_addrs))


def test_mailer_speaks_smtp_with_tls_and_login(monkeypatch):
    _FakeSMTP.log = []
    monkeypatch.setattr(mailer.smtplib, "SMTP", _FakeSMTP)
    monkeypatch.setattr(mailer.smtplib, "SMTP_SSL", type("_FakeSSL", (_FakeSMTP,), {}))
    for k, v in {"PRODUCT_SMTP_HOST": "smtp.test", "PRODUCT_SMTP_PORT": "2525", "PRODUCT_SMTP_USER": "u",
                 "PRODUCT_SMTP_PASSWORD": "p", "PRODUCT_SMTP_FROM": "DevOps <no-reply@example.com>"}.items():
        monkeypatch.setenv(k, v)
    mailer.send("a@example.com", "Hi", "body")
    assert _FakeSMTP.log == [("connect", "_FakeSMTP", "smtp.test", 2525, 15), ("starttls", True), ("login", "u", "p"),
                             ("send", "DevOps <no-reply@example.com>", "a@example.com", "Hi", "body",
                              "no-reply@example.com", ["a@example.com"])]
    _FakeSMTP.log.clear()
    monkeypatch.setenv("PRODUCT_SMTP_PORT", "465")  # implicit TLS: no STARTTLS step
    mailer.send("a@example.com", "Hi", "body")
    assert _FakeSMTP.log[0][1] == "_FakeSSL" and ("starttls", True) not in _FakeSMTP.log
    _FakeSMTP.log.clear()
    monkeypatch.setenv("PRODUCT_SMTP_PORT", "25")
    monkeypatch.setenv("PRODUCT_SMTP_STARTTLS", "off")
    monkeypatch.delenv("PRODUCT_SMTP_USER")
    mailer.send("a@example.com", "Hi", "body")
    assert [e[0] for e in _FakeSMTP.log] == ["connect", "send"]


def test_mail_failure_never_breaks_or_leaks_through_the_recovery_request(mail_env, caplog):
    c, mp, _ = mail_env

    def broken(to, subject, body):
        raise OSError("smtp down: " + body)  # a careless error text must not reach the log

    mp.setattr(mailer, "send", broken)
    _signup(c, email="m@example.com")
    with caplog.at_level("WARNING", logger="product.mailer"):
        r = c.post("/recover", json={"email": "m@example.com"})
    assert r.status_code == 202  # the caller cannot tell a mail failure from success (and could not before either)
    assert "OSError" in caplog.text and "recover#t=" not in caplog.text and "smtp down" not in caplog.text


def test_llm_token_quota_is_enforced_on_hard_limit_plans(env):
    c, mp = env
    mp.setenv("PRODUCT_PLANS", '{"free": {"monthly_quota": {"analyze": 50, "llm_tokens": 100}}}')
    t = _tenant(c)
    assert _ask(c, t["api_key"]).status_code == 200
    store.record(t["id"], "llm_tokens", 100)
    r = _ask(c, t["api_key"])
    assert r.status_code == 429 and "llm_tokens" in r.json()["detail"]


def test_paid_plan_spend_cap(env):
    c, mp = env
    mp.setenv("PRODUCT_PLANS", '{"starter": {"monthly_quota": {"analyze": 1}, "spend_cap_cents": 10}}')
    s = _tenant(c, "starter", "s")  # overage 5 cents/run: runs 2 and 3 reach the cap, run 4 would pass it
    codes = [_ask(c, s["api_key"]) for _ in range(4)]
    assert [r.status_code for r in codes] == [200, 200, 200, 429] and "spend cap" in codes[3].json()["detail"]


def test_quota_check_and_write_are_atomic(env):
    import threading
    from fastapi import HTTPException
    from product import metering
    c, mp = env
    mp.setenv("PRODUCT_PLANS", '{"free": {"monthly_quota": {"analyze": 5}}}')
    t = _tenant(c)
    ok, refused = [], []

    def go():
        try:
            ok.append(metering.reserve(t, "analyze"))
        except HTTPException:
            refused.append(1)

    threads = [threading.Thread(target=go) for _ in range(20)]
    [x.start() for x in threads]
    [x.join() for x in threads]
    assert len(ok) == 5 and len(refused) == 15
    assert store.usage_since(t["id"], store.month_start())["analyze"] == 5


def test_failed_run_releases_its_reservation(env):
    c, mp = env
    import api.main as m
    mp.setattr(m, "_run", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    t = _tenant(c)
    try:
        _ask(c, t["api_key"])
    except RuntimeError:
        pass
    assert store.usage_since(t["id"], store.month_start()).get("analyze", 0) == 0


def test_stripe_report_stamps_usage_into_its_own_month(env):
    import calendar
    import time as _t
    c, mp = env
    t = _tenant(c)
    mp.setenv("STRIPE_SECRET_KEY", "sk_test_x")
    mp.setenv("STRIPE_METER_ANALYZE", "agent_analyze_runs")
    c.patch(f"/admin/tenants/{t['id']}", json={"stripe_customer_id": "cus_1"}, headers={"x-admin-key": "adm"})
    jan, feb = calendar.timegm((2026, 1, 31, 23, 59, 0)), calendar.timegm((2026, 2, 1, 0, 1, 0))
    with store.conn() as db:
        for ts in (jan, feb):
            db.execute("INSERT INTO usage(tenant_id,event,quantity,ts) VALUES(?,?,?,?)", (t["id"], "analyze", 1, ts))
    calls = []

    class R:
        status_code = 200

    assert billing.report_usage(post=lambda d: (calls.append(d), R())[1]) == {"reported": 2, "failed": 0}
    assert sorted(int(d["timestamp"]) for d in calls) == [jan, feb]
    assert billing.report_usage(post=lambda d: R())["reported"] == 0


# ---- legal documents and the signup gate -----------------------------------------------------------

def _legal_file(tmp_path, monkeypatch, **over):
    f = tmp_path / "e.json"
    f.write_text(json.dumps({**{k: f"v-{k}" for k in legal.FIELDS}, "COUNSEL_REVIEWED": True, **over}))
    monkeypatch.setenv("PRODUCT_LEGAL_FILE", str(f))


def test_legal_pages_and_signup_stay_closed_until_the_setup_is_complete(env, tmp_path):
    c, mp = env
    mp.delenv("PRODUCT_LEGAL_FILE")  # the shipped entity.json has no values and COUNSEL_REVIEWED=false
    assert not legal.configured() and "COUNSEL_REVIEWED" in legal.missing()
    for slug in legal.TITLES:
        r = c.get(f"/{slug}")
        assert r.status_code == 503 and "{{" not in r.text and "not been published" in r.text
    assert c.post("/signup", json={"name": "x", "email": "a@example.com", "accept_terms": True}).status_code == 403
    assert "Sign up" in c.get("/pricing").text and "Start free" not in c.get("/pricing").text
    _legal_file(tmp_path, mp, LEGAL_EMAIL="")  # one blank field is enough
    assert legal.missing() == ["LEGAL_EMAIL"] and c.get("/terms").status_code == 503
    _legal_file(tmp_path, mp, COUNSEL_REVIEWED=False)  # filled in but not approved
    assert legal.missing() == ["COUNSEL_REVIEWED"] and c.get("/privacy").status_code == 503
    assert _signup(c).status_code == 403


def test_legal_pages_render_the_entity_values_escaped_and_are_linked_everywhere(env, tmp_path):
    c, mp = env
    _legal_file(tmp_path, mp, LEGAL_NAME="Acme <script>alert(1)</script> Ltd", LIABILITY_CAP="USD 100",
                REFUND_WINDOW_DAYS=14)
    for slug, title in legal.TITLES.items():
        r = c.get(f"/{slug}")
        assert r.status_code == 200 and title in r.text and "{{" not in r.text
        assert "<script>alert" not in r.text
        assert slug == "refunds" or "Acme &lt;script&gt;" in r.text  # the refund page never names the company
        assert r.headers["content-security-policy"].startswith("default-src 'none'")
        for other in legal.TITLES:  # footer on every page
            assert f'href="/{other}"' in r.text
    assert "USD 100" in c.get("/terms").text and "14 days" in c.get("/refunds").text
    assert 'href="/terms"' in c.get("/pricing").text and 'href="/privacy"' in c.get("/signup").text


def test_signup_needs_explicit_acceptance_and_records_the_version(env):
    c, _ = env
    body = {"name": "x", "email": "ok@example.com"}
    for bad in ({}, {"accept_terms": False}, {"accept_terms": "true"}, {"accept_terms": 1}):
        r = c.post("/signup", json={**body, **bad})
        assert r.status_code == 422 and "accept_terms" in r.text
    assert "I accept the" in c.get("/signup").text and "accept_terms" in c.get("/signup").text
    t = c.post("/signup", json={**body, "accept_terms": True}).json()
    with store.conn() as db:
        row = db.execute("SELECT terms_version, terms_accepted_at FROM tenants WHERE id=?", (t["id"],)).fetchone()
    assert row[0] == legal.VERSION and row[1] > 0
    admin = _tenant(c)  # operator-created tenants have a separate contract; nothing is claimed for them
    with store.conn() as db:
        assert tuple(db.execute("SELECT terms_version, terms_accepted_at FROM tenants WHERE id=?",
                                (admin["id"],)).fetchone()) == (None, None)


def test_shipped_legal_texts_only_use_known_tokens():
    shipped = [legal.source(s) for s in legal.TITLES] + [f.read_text() for f in legal.FILES_WITH_TOKENS]
    used = {tok for text in shipped for tok in legal.TOKEN.findall(text)}
    assert used <= set(legal.FIELDS), used - set(legal.FIELDS)
    assert legal.fill("{{LEGAL_NAME}} {{VENUE}}", {"LEGAL_NAME": " A  B "}) == "A B {{VENUE}}"  # blanks stay visible
    assert legal.unresolved_tokens()  # the shipped licence files still have tokens until `python -m product.legal fill`


# ---- email verification at signup ---------------------------------------------------------------

def _vtoken(sent):
    return _re.search(r"https://agent\.example\.com/verify#t=([A-Za-z0-9_-]+)", sent.verification[-1][2]).group(1)


def test_signup_mails_a_link_that_verifies_the_email_once(mail_env):
    c, _, sent = mail_env
    t = _signup(c, email="v@example.com").json()
    assert t["email_verified"] is False and len(sent.verification) == 1 and sent.verification[0][0] == "v@example.com"
    key = {"x-api-key": t["api_key"]}
    assert c.get("/billing", headers=key).json()["email_verified"] is False
    tok = _vtoken(sent)
    assert c.get("/verify").status_code == 200  # opening the page (a mail scanner) spends nothing
    assert c.get("/billing", headers=key).json()["email_verified"] is False
    assert c.post("/verify/confirm", json={"token": tok}).status_code == 200
    assert c.get("/billing", headers=key).json()["email_verified"] is True
    assert c.post("/verify/confirm", json={"token": tok}).status_code == 400  # single use
    assert c.post("/verify/confirm", json={"token": "junk"}).status_code == 400
    assert c.post("/verify/resend", headers=key).json() == {"status": "already verified"}


def test_unverified_email_is_taken_over_only_after_the_grace_period(mail_env):
    c, mp, sent = mail_env
    squatter = _signup(c, email="victim@example.com").json()
    assert _signup(c, email="victim@example.com").status_code == 409  # inside the grace period
    mp.setenv("PRODUCT_VERIFY_GRACE", "0")
    owner = _signup(c, email="victim@example.com")
    assert owner.status_code == 200 and store.tenant_for_email("victim@example.com")["id"] == owner.json()["id"]
    assert _ask(c, squatter["api_key"]).status_code == 200  # the old account keeps its keys, only the address moves
    # a verified address is never taken over, however short the grace period
    tok = _vtoken(sent)
    c.post("/verify/confirm", json={"token": tok})
    assert _signup(c, email="victim@example.com").status_code == 409


def test_resend_is_throttled_and_recovery_also_verifies(mail_env):
    c, _, sent = mail_env
    t = _signup(c, email="r@example.com").json()
    key = {"x-api-key": t["api_key"]}
    assert c.post("/verify/resend", headers=key).status_code == 202 and len(sent.verification) == 1  # cooldown: no 2nd mail
    c.post("/recover", json={"email": "r@example.com"})
    assert c.post("/recover/confirm", json={"token": _token(sent)}).status_code == 200
    assert store.get_tenant(t["id"])["email_verified_at"] is not None


def test_no_verification_mail_without_mail_config(env):
    c, _ = env
    r = _signup(c).json()
    assert r["email_verified"] is False
    with store.conn() as db:
        assert db.execute("SELECT COUNT(*) FROM verify_tokens").fetchone()[0] == 0
