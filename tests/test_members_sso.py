"""Members (seats), invitations, per-seat Stripe quantity and per-tenant OIDC sign-in."""
import base64
import hashlib
import json
import re
import sqlite3
import time
from contextlib import contextmanager
from urllib.parse import parse_qs, urlparse

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient

from api.main import app
from product import mailer, sso, store, subscriptions

ISSUER = "https://idp.test"
CLIENT_ID = "client-1"
SECRET = "s3cret-client-secret"
ADMIN = {"x-admin-key": "adm"}
_n = iter(range(10**6))


@pytest.fixture
def team(tmp_path, monkeypatch):
    monkeypatch.setenv("PRODUCT_DB", str(tmp_path / "p.db"))
    monkeypatch.setenv("PRODUCT_ADMIN_KEY", "adm")
    monkeypatch.setenv("AGENT_LLM", "off")
    monkeypatch.setenv("PRODUCT_SMTP_HOST", "smtp.test")
    monkeypatch.setenv("PRODUCT_SMTP_FROM", "agent@example.test")
    monkeypatch.setenv("PRODUCT_PUBLIC_URL", "http://testserver")  # http: the test client does not send Secure cookies
    monkeypatch.setenv("PRODUCT_SECRET_KEY", "k" * 40)
    monkeypatch.setenv("PRODUCT_SSO_ALLOW_HTTP", "1")  # no DNS in tests; validate_url has its own tests
    for k in ("AGENT_API_KEY", "STRIPE_SECRET_KEY", "PRODUCT_PLANS"):
        monkeypatch.delenv(k, raising=False)
    mails = []
    monkeypatch.setattr(mailer, "send", lambda to, subject, body: mails.append((to, subject, body)))
    sso._disco.clear()
    sso._jwks.clear()
    return TestClient(app), monkeypatch, mails


def _tenant(c, plan="starter", email=None, name="acme"):
    email = email or f"owner{next(_n)}@example.com"
    r = c.post("/admin/tenants", json={"name": name, "plan": plan, "email": email}, headers=ADMIN)
    assert r.status_code == 200, r.text
    return r.json()


def _h(t):
    return {"x-api-key": t["api_key"]}


def _invite(c, t, mails, email, role="member"):
    r = c.post("/members", json={"email": email, "role": role}, headers=_h(t))
    return r, (re.search(r"#t=([\w-]+)", mails[-1][2]).group(1) if r.status_code == 202 else None)


def _join(t, mails, email, role="member"):
    """Invite `email` and accept in a fresh browser; returns (client, accept response)."""
    c = TestClient(app)
    r, token = _invite(c, t, mails, email, role)
    assert r.status_code == 202, r.text
    return c, c.post("/invite/accept", json={"token": token})


# ---- members and invitations ----------------------------------------------------------------------------

def test_signup_email_becomes_the_owner_seat(team):
    c, _, _ = team
    t = _tenant(c, email="boss@example.com")
    r = c.get("/members", headers=_h(t)).json()
    assert [(m["email"], m["role"], m["status"]) for m in r["members"]] == [("boss@example.com", "owner", "active")]
    assert r["seats"] == {"billed": 1, "max": 10, "price_cents": 1500, "currency": "usd"}
    assert "oidc_subject" not in r["members"][0] and r["members"][0]["sso_linked"] is False
    assert c.get("/members").status_code == 401


def test_invite_accept_makes_an_active_seat_and_signs_the_member_in(team):
    c, _, mails = team
    t = _tenant(c)
    r, token = _invite(c, t, mails, "Dev@Example.com")
    assert r.status_code == 202 and r.json()["member"]["status"] == "invited"
    to, subject, body = mails[-1]
    assert to == "dev@example.com" and "acme" in body and "7 days" in body and "invited" in subject.lower()
    assert store.seat_count(t["id"]) == 1  # a pending invitation is not billed
    dev = TestClient(app)
    ok = dev.post("/invite/accept", json={"token": token})
    assert ok.status_code == 200 and ok.json()["signed_in"] is True and ok.json()["member"]["status"] == "active"
    assert store.seat_count(t["id"]) == 2
    me = dev.get("/me").json()  # the session cookie from accepting
    assert me["via"] == "session" and me["role"] == "member" and me["member"]["email"] == "dev@example.com"
    assert dev.get("/members").status_code == 200 and dev.get("/usage").status_code == 200
    assert dev.get("/billing").json()["can_manage"] is False and dev.get("/billing").json()["purchasable"] == []
    assert dev.post("/invite/accept", json={"token": token}).status_code == 400  # single use


def test_plain_members_can_look_but_not_manage(team):
    c, _, mails = team
    t = _tenant(c)
    dev, _ = _join(t, mails, "dev@example.com")
    mid = dev.get("/me").json()["member"]["id"]
    assert dev.post("/members", json={"email": "x@example.com"}).status_code == 403
    assert dev.delete(f"/members/{mid}").status_code == 403
    assert dev.put("/sso", json={"enabled": False}).status_code == 403


def test_invitations_need_mail_and_are_limited(team):
    c, mp, mails = team
    t = _tenant(c)
    assert c.post("/members", json={"email": "bad"}, headers=_h(t)).status_code == 422
    assert c.post("/members", json={"email": "a@example.com", "role": "owner"}, headers=_h(t)).status_code == 422
    mp.setenv("PRODUCT_INVITES_PER_TENANT_HOUR", "2")
    assert _invite(c, t, mails, "a@example.com")[0].status_code == 202
    assert _invite(c, t, mails, "b@example.com")[0].status_code == 202
    assert _invite(c, t, mails, "c@example.com")[0].status_code == 429
    mp.delenv("PRODUCT_SMTP_HOST")
    assert c.post("/members", json={"email": "d@example.com"}, headers=_h(t)).status_code == 503


def test_resend_cooldown_and_already_a_member(team):
    c, _, mails = team
    t = _tenant(c)
    assert _invite(c, t, mails, "a@example.com")[0].status_code == 202
    assert _invite(c, t, mails, "a@example.com")[0].status_code == 409  # just sent: no mailbombing
    assert len(mails) == 1
    _join(t, mails, "b@example.com")
    r, _ = _invite(c, t, mails, "b@example.com")
    assert r.status_code == 409 and "already a member" in r.json()["detail"]


def test_plan_seat_cap_counts_open_invitations_and_is_rechecked_on_accept(team):
    c, mp, mails = team
    free = _tenant(c, plan="free")
    assert c.post("/members", json={"email": "a@example.com"}, headers=_h(free)).status_code == 402  # free: 1 seat
    mp.setenv("PRODUCT_PLANS", '{"starter": {"max_seats": 3}}')
    t = _tenant(c)
    _, tok_a = _invite(c, t, mails, "a@example.com")
    assert _invite(c, t, mails, "b@example.com")[0].status_code == 202  # owner + a + b = 3
    r, _ = _invite(c, t, mails, "c@example.com")
    assert r.status_code == 402 and "allows 3 members" in r.json()["detail"]
    mp.setenv("PRODUCT_PLANS", '{"starter": {"max_seats": 1}}')  # plan shrank after the invitation went out
    r = c.post("/invite/accept", json={"token": tok_a})
    assert r.status_code == 402 and store.seat_count(t["id"]) == 1


def test_expired_invitation_is_refused_and_frees_its_place(team):
    c, mp, mails = team
    mp.setenv("PRODUCT_PLANS", '{"starter": {"max_seats": 2}}')
    t = _tenant(c)
    _, token = _invite(c, t, mails, "a@example.com")
    with sqlite3.connect(store.db_path()) as db:
        db.execute("UPDATE member_invites SET expires_at=?", (time.time() - 1,))
    assert c.post("/invite/accept", json={"token": token}).status_code == 400
    assert _invite(c, t, mails, "b@example.com")[0].status_code == 202  # a's expired invitation no longer holds a place


def test_removing_a_member_ends_their_session_and_frees_the_seat(team):
    c, _, mails = team
    t = _tenant(c)
    dev, _ = _join(t, mails, "dev@example.com")
    mid = dev.get("/me").json()["member"]["id"]
    assert store.seat_count(t["id"]) == 2
    assert c.delete(f"/members/{mid}", headers=_h(t)).json() == {"removed": mid}
    assert dev.get("/me").status_code == 401 and store.seat_count(t["id"]) == 1
    assert c.delete(f"/members/{mid}", headers=_h(t)).status_code == 404
    again, _ = _invite(c, t, mails, "dev@example.com")  # a removed person can be invited again
    assert again.status_code == 202


def test_owner_rules(team):
    c, _, mails = team
    t = _tenant(c, email="boss@example.com")
    owner = c.get("/members", headers=_h(t)).json()["members"][0]
    assert c.delete(f"/members/{owner['id']}", headers=_h(t)).status_code == 409  # the last owner stays
    assert c.patch(f"/members/{owner['id']}", json={"role": "member"}, headers=_h(t)).status_code == 409
    admin, _ = _join(t, mails, "adm@example.com", role="admin")
    assert admin.get("/me").json()["role"] == "admin"
    assert admin.delete(f"/members/{owner['id']}").status_code == 403  # an admin cannot remove an owner
    mid = admin.get("/me").json()["member"]["id"]
    assert admin.patch(f"/members/{mid}", json={"role": "owner"}).status_code == 403  # nor make owners
    assert c.patch(f"/members/{mid}", json={"role": "owner"}, headers=_h(t)).json()["member"]["role"] == "owner"
    assert c.delete(f"/members/{owner['id']}", headers=_h(t)).status_code == 200  # now there is a second owner


def test_session_posts_from_another_origin_are_refused(team):
    c, _, mails = team
    t = _tenant(c)
    adm, _ = _join(t, mails, "adm@example.com", role="admin")
    body = {"email": "x@example.com"}
    assert adm.post("/members", json=body, headers={"origin": "https://evil.example"}).status_code == 403
    assert adm.post("/members", json=body, headers={"origin": "http://testserver"}).status_code == 202


def test_a_wrong_key_is_never_rescued_by_a_cookie(team):
    c, _, mails = team
    t = _tenant(c)
    dev, _ = _join(t, mails, "dev@example.com")
    assert dev.get("/me", headers={"x-api-key": "adk_wrong"}).status_code == 401


def test_cookies_are_signed_per_purpose(team):
    c, _, _ = team
    t = _tenant(c)
    mid = c.get("/members", headers=_h(t)).json()["members"][0]["id"]
    forged = TestClient(app)
    forged.cookies.set(sso.SESSION_COOKIE, sso.sign({"t": t["id"], "m": mid, "exp": time.time() + 99}, "flow"))
    assert forged.get("/me").status_code == 401  # a flow cookie is not a session
    forged.cookies.set(sso.SESSION_COOKIE, sso.sign({"t": t["id"], "m": mid, "exp": time.time() - 1}, "session"))
    assert forged.get("/me").status_code == 401  # expired
    forged.cookies.set(sso.SESSION_COOKIE, sso.make_session(t["id"], mid)[:-2] + "xx")
    assert forged.get("/me").status_code == 401  # bad signature
    forged.cookies.set(sso.SESSION_COOKIE, sso.make_session(t["id"], mid))
    assert forged.get("/me").json()["role"] == "owner"


# ---- Stripe: seat quantity ------------------------------------------------------------------------------

class _R:
    def __init__(self, body, code=200):
        self._b, self.status_code = body, code

    def json(self):
        return self._b


class FakeSubscription:
    """One Stripe subscription: base price item plus (optionally) a seat item; records every write."""

    def __init__(self, seat_qty=None):
        self.items = [{"id": "si_base", "price": {"id": "price_starter"}, "quantity": 1}]
        if seat_qty is not None:
            self.items.append({"id": "si_seat", "price": {"id": "price_starter_seat"}, "quantity": seat_qty})
        self.writes = []

    def get(self, url, auth, timeout):
        return _R({"id": "sub_1", "items": {"data": self.items}})

    def post(self, url, data, headers, auth, timeout):
        if url.endswith("/customers"):  # creating a tenant also creates its Stripe customer
            return _R({"id": "cus_1"})
        self.writes.append((url.split("/v1")[1], dict(data)))
        if url.endswith("/subscription_items"):
            self.items.append({"id": "si_seat", "price": {"id": data["price"]}, "quantity": int(data["quantity"])})
        else:
            next(i for i in self.items if url.endswith(i["id"]))["quantity"] = int(data["quantity"])
        return _R({})


@pytest.fixture
def billed(team):
    c, mp, mails = team
    mp.setenv("STRIPE_SECRET_KEY", "sk_test")
    mp.setenv("STRIPE_PRICE_STARTER", "price_starter,price_starter_seat:seat")
    t = _tenant(c)
    store.update_tenant(t["id"], stripe_subscription_id="sub_1", subscription_status="active")
    fake = FakeSubscription()
    mp.setattr(subscriptions.httpx, "get", fake.get)
    mp.setattr(subscriptions.httpx, "post", fake.post)
    return c, mp, mails, t, fake


def test_seat_price_is_added_then_follows_the_member_count(billed):
    c, _, mails, t, fake = billed
    assert subscriptions.sync_seats(t["id"]) == {"status": "synced", "seats": 1}  # subscription predates seat pricing
    assert fake.writes == [("/subscription_items", {"subscription": "sub_1", "price": "price_starter_seat",
                                                    "quantity": "1", "proration_behavior": "create_prorations"})]
    assert subscriptions.sync_seats(t["id"])["status"] == "unchanged" and len(fake.writes) == 1
    _join(t, mails, "dev@example.com")  # accepting syncs in the background
    assert fake.writes[-1] == ("/subscription_items/si_seat", {"quantity": "2", "proration_behavior": "create_prorations"})
    assert store.get_tenant(t["id"])["seats_synced"] == 2
    mid = c.get("/members", headers=_h(t)).json()["members"][-1]["id"]
    c.delete(f"/members/{mid}", headers=_h(t))
    assert fake.writes[-1][1]["quantity"] == "1" and store.get_tenant(t["id"])["seats_synced"] == 1


def test_pending_invitation_does_not_change_the_bill(billed):
    c, _, mails, t, fake = billed
    subscriptions.sync_seats(t["id"])
    n = len(fake.writes)
    _invite(c, t, mails, "dev@example.com")
    assert len(fake.writes) == n


def test_a_failed_sync_is_left_for_reconcile(billed):
    c, mp, mails, t, fake = billed
    subscriptions.sync_seats(t["id"])
    mp.setattr(subscriptions.httpx, "post", lambda *a, **k: (_ for _ in ()).throw(subscriptions.httpx.ConnectError("down")))
    _join(t, mails, "dev@example.com")  # the invitation still works while Stripe is down
    assert store.seat_count(t["id"]) == 2 and store.get_tenant(t["id"])["seats_synced"] == 1
    assert store.seat_drift() == [{"tenant_id": t["id"], "synced": 1, "actual": 2}]
    mp.setattr(subscriptions.httpx, "post", fake.post)
    assert subscriptions.reconcile_seats() == {"seats_synced": 1, "seats_failed": 0}
    assert store.seat_drift() == [] and fake.items[-1]["quantity"] == 2
    assert subscriptions.reconcile_seats() == {"seats_synced": 0, "seats_failed": 0}


def test_sync_is_skipped_without_a_subscription_or_seat_price(billed):
    c, mp, _, t, fake = billed
    free = _tenant(c, plan="free")
    assert subscriptions.sync_seats(free["id"])["status"] == "skipped"
    mp.setenv("STRIPE_PRICE_STARTER", "price_starter")  # no :seat price configured
    assert subscriptions.sync_seats(t["id"])["status"] == "skipped" and fake.writes == []
    mp.delenv("STRIPE_SECRET_KEY")
    assert subscriptions.sync_seats(t["id"])["status"] == "skipped"


def test_checkout_bills_one_seat_per_active_member_and_never_sells_seats_for_free(team):
    c, mp, mails = team
    mp.setenv("STRIPE_SECRET_KEY", "sk_test")
    mp.setenv("STRIPE_PRICE_STARTER", "price_starter,price_starter_seat:seat")
    t = _tenant(c)
    _join(t, mails, "dev@example.com")
    sent = []
    fake = lambda url, data, headers: sent.append(data) or _R({"id": "cus_1", "url": "https://stripe/checkout"})  # noqa: E731
    subscriptions.create_checkout(store.get_tenant(t["id"]), "starter", "https://x/ok", "https://x/no", post=fake)
    d = sent[-1]
    assert d["line_items[0][price]"] == "price_starter" and d["line_items[0][quantity]"] == "1"
    assert d["line_items[1][price]"] == "price_starter_seat" and d["line_items[1][quantity]"] == "2"
    mp.setenv("STRIPE_PRICE_STARTER", "price_starter")
    with pytest.raises(subscriptions.StripeError, match="no :seat price"):
        subscriptions.create_checkout(store.get_tenant(t["id"]), "starter", "https://x/ok", "https://x/no", post=fake)


def test_webhook_records_the_seat_quantity_stripe_bills(team):
    c, mp, _ = team
    mp.setenv("STRIPE_SECRET_KEY", "sk_test")
    mp.setenv("STRIPE_PRICE_STARTER", "price_starter,price_starter_seat:seat")
    t = _tenant(c, plan="free")

    def event(items, eid):
        return {"id": eid, "type": "customer.subscription.updated", "data": {"object": {
            "id": "sub_1", "customer": "cus_1", "status": "active", "metadata": {"tenant_id": t["id"]},
            "items": {"data": items}}}}

    base = {"id": "si_base", "price": {"id": "price_starter"}}
    subscriptions.handle_event(event([base, {"id": "si_seat", "price": {"id": "price_starter_seat"}, "quantity": 3}], "e1"))
    assert store.get_tenant(t["id"])["plan"] == "starter" and store.get_tenant(t["id"])["seats_synced"] == 3
    subscriptions.handle_event(event([base], "e2"))  # a plan change dropped the seat item: unknown, so reconcile adds it
    assert store.get_tenant(t["id"])["seats_synced"] is None
    assert [d["tenant_id"] for d in store.seat_drift()] == [t["id"]]


def test_admin_can_trigger_a_reconcile(billed):
    c, _, _, t, fake = billed
    assert c.post("/admin/billing/seats").status_code == 401
    assert c.post("/admin/billing/seats", headers=ADMIN).json() == {"seats_synced": 1, "seats_failed": 0}
    assert fake.items[-1]["quantity"] == 1


# ---- OIDC --------------------------------------------------------------------------------------------------

KEY = rsa.generate_private_key(65537, 2048)
JWK = {**json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(KEY.public_key())), "kid": "k1", "use": "sig", "alg": "RS256"}


def _b64(b):
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


class FakeIdP:
    """Discovery, JWKS and token endpoint of one IdP. Enforces PKCE on the token request like a real one."""

    def __init__(self, mp):
        self.sub, self.email, self.claims, self.auth, self.token_requests = "u-1", "dev@example.com", {}, {}, []
        self.alg, self.key = "RS256", KEY
        mp.setattr(sso, "fetch_json", self.fetch)
        mp.setattr(sso, "post_form", self.post)

    def fetch(self, url):
        if url == ISSUER + "/.well-known/openid-configuration":
            return {"issuer": ISSUER, "authorization_endpoint": ISSUER + "/authorize", "token_endpoint": ISSUER + "/token",
                    "jwks_uri": ISSUER + "/jwks"}
        if url == ISSUER + "/jwks":
            return {"keys": [JWK]}
        raise sso.SSOError("unexpected fetch " + url)

    def token(self, **over):
        now = int(time.time())
        claims = {"iss": ISSUER, "aud": CLIENT_ID, "sub": self.sub, "email": self.email, "email_verified": True,
                  "nonce": self.auth.get("nonce"), "iat": now, "exp": now + 300, **self.claims, **over}
        return jwt.encode({k: v for k, v in claims.items() if v is not None}, self.key, algorithm=self.alg,
                          headers={"kid": "k1"})

    def post(self, url, data):
        assert url == ISSUER + "/token"
        self.token_requests.append(data)
        assert _b64(hashlib.sha256(data["code_verifier"].encode()).digest()) == self.auth["code_challenge"]  # PKCE
        assert data["redirect_uri"] == "http://testserver/sso/callback" and data["grant_type"] == "authorization_code"
        return {"id_token": self.token()}


@pytest.fixture
def idp_team(team):
    c, mp, mails = team
    idp = FakeIdP(mp)
    t = _tenant(c, plan="pro", email="boss@example.com")
    r = c.put("/sso", json={"issuer": ISSUER, "client_id": CLIENT_ID, "client_secret": SECRET}, headers=_h(t))
    assert r.status_code == 200, r.text
    return c, mp, mails, t, idp


def _sso_login(idp, client, tid, state=None):
    r = client.get(f"/sso/{tid}/login", follow_redirects=False)
    assert r.status_code == 302, r.text
    loc = urlparse(r.headers["location"])
    q = {k: v[0] for k, v in parse_qs(loc.query).items()}
    idp.auth = q
    assert (loc.scheme + "://" + loc.netloc + loc.path) == ISSUER + "/authorize"
    assert q["response_type"] == "code" and q["client_id"] == CLIENT_ID and q["code_challenge_method"] == "S256"
    assert q["redirect_uri"] == "http://testserver/sso/callback" and "openid" in q["scope"].split()
    return client.get("/sso/callback", params={"code": "abc", "state": state or q["state"]}, follow_redirects=False)


def test_sso_settings_hide_the_secret_and_say_where_to_register(idp_team):
    c, _, _, t, _ = idp_team
    v = c.get("/sso", headers=_h(t)).json()
    assert v == {"configured": True, "issuer": ISSUER, "client_id": CLIENT_ID, "enabled": True,
                 "redirect_uri": "http://testserver/sso/callback", "login_url": f"http://testserver/sso/{t['id']}/login"}
    assert SECRET not in json.dumps(v)
    with sqlite3.connect(store.db_path()) as db:  # encrypted at rest
        stored = db.execute("SELECT client_secret_enc FROM tenant_sso").fetchone()[0]
    assert SECRET not in stored and sso.decrypt_secret(stored) == SECRET


def test_sso_is_a_paid_feature_and_validates_its_input(team):
    c, mp, _ = team
    FakeIdP(mp)
    cfg = {"issuer": ISSUER, "client_id": CLIENT_ID, "client_secret": SECRET}
    assert c.put("/sso", json=cfg, headers=_h(_tenant(c, plan="free"))).status_code == 402
    t = _tenant(c, plan="pro")
    assert c.put("/sso", json={}, headers=_h(t)).status_code == 422
    assert c.put("/sso", json={"issuer": ISSUER, "client_id": CLIENT_ID}, headers=_h(t)).status_code == 422  # secret needed
    assert c.put("/sso", json={**cfg, "enabled": "yes"}, headers=_h(t)).status_code == 422
    assert c.put("/sso", json={**cfg, "issuer": "https://other.test"}, headers=_h(t)).status_code == 422  # discovery fails
    mp.setenv("PRODUCT_SECRET_KEY", "short")
    assert c.put("/sso", json=cfg, headers=_h(t)).status_code == 503


def test_discovery_whose_issuer_differs_is_refused(idp_team):
    c, mp, _, t, idp = idp_team
    sso._disco.clear()
    mp.setattr(idp, "fetch", lambda url: {"issuer": "https://evil.test", "authorization_endpoint": "https://x/a",
                                          "token_endpoint": "https://x/t", "jwks_uri": "https://x/j"})
    mp.setattr(sso, "fetch_json", idp.fetch)
    r = c.put("/sso", json={"issuer": ISSUER, "client_id": CLIENT_ID, "client_secret": SECRET}, headers=_h(t))
    assert r.status_code == 422 and "does not match" in r.json()["detail"]


def test_invited_member_signs_in_with_sso_and_takes_a_seat(idp_team):
    c, _, mails, t, idp = idp_team
    assert _invite(c, t, mails, "dev@example.com")[0].status_code == 202
    assert store.seat_count(t["id"]) == 1
    dev = TestClient(app)
    r = _sso_login(idp, dev, t["id"])
    assert r.status_code == 303 and r.headers["location"] == "/account"
    me = dev.get("/me").json()
    assert me["via"] == "session" and me["member"]["email"] == "dev@example.com" and me["member"]["sso_linked"] is True
    assert store.seat_count(t["id"]) == 2
    assert idp.token_requests[0]["client_secret"] == SECRET  # decrypted for the token request only


def test_bound_subject_survives_an_email_change_and_cannot_be_taken_by_email(idp_team):
    c, _, mails, t, idp = idp_team
    _join(t, mails, "dev@example.com")  # accepted by email link, not yet bound to an IdP identity
    assert _sso_login(idp, TestClient(app), t["id"]).status_code == 303  # first login binds sub u-1
    idp.email = "renamed@example.com"
    assert _sso_login(idp, TestClient(app), t["id"]).status_code == 303  # matched by subject now
    idp.sub, idp.email = "u-2", "dev@example.com"  # someone else claiming the bound member's email
    assert _sso_login(idp, TestClient(app), t["id"]).status_code == 403


def test_sso_never_adds_people_and_removed_members_stay_out(idp_team):
    c, _, mails, t, idp = idp_team
    stranger = TestClient(app)
    r = _sso_login(idp, stranger, t["id"])
    assert r.status_code == 403 and "invite" in r.text and stranger.get("/me").status_code == 401
    assert store.seat_count(t["id"]) == 1
    _join(t, mails, "dev@example.com")  # an SSO team's invitation does not sign the person in
    mid = next(m["id"] for m in c.get("/members", headers=_h(t)).json()["members"] if m["email"] == "dev@example.com")
    c.delete(f"/members/{mid}", headers=_h(t))
    assert _sso_login(idp, TestClient(app), t["id"]).status_code == 403


def test_unverified_idp_email_is_not_used_to_match(idp_team):
    c, _, mails, t, idp = idp_team
    _invite(c, t, mails, "dev@example.com")
    idp.claims = {"email_verified": False}
    assert _sso_login(idp, TestClient(app), t["id"]).status_code == 403
    idp.claims = {"email_verified": None, "email": "dev@example.com"}  # claim absent (Entra): accepted
    assert _sso_login(idp, TestClient(app), t["id"]).status_code == 303


@pytest.mark.parametrize("claims", [
    {"aud": "someone-else"}, {"iss": "https://evil.test"}, {"nonce": "replayed"}, {"nonce": None},
    {"exp": 1}, {"sub": None}, {"iat": None},
])
def test_bad_id_tokens_are_refused(idp_team, claims):
    c, _, mails, t, idp = idp_team
    _invite(c, t, mails, "dev@example.com")
    idp.claims = claims
    dev = TestClient(app)
    r = _sso_login(idp, dev, t["id"])
    assert r.status_code == 400 and dev.get("/me").status_code == 401
    assert store.seat_count(t["id"]) == 1


def test_unsigned_wrongly_signed_and_hmac_tokens_are_refused(idp_team):
    c, _, mails, t, idp = idp_team
    _invite(c, t, mails, "dev@example.com")
    other = rsa.generate_private_key(65537, 2048)
    idp.key = other  # signed by a key that is not in the IdP's JWKS
    assert _sso_login(idp, TestClient(app), t["id"]).status_code == 400
    idp.key, idp.alg = "x" * 40, "HS256"  # algorithm confusion: HMAC with a guessable secret
    assert _sso_login(idp, TestClient(app), t["id"]).status_code == 400
    idp.key, idp.alg = KEY, "RS256"
    assert _sso_login(idp, TestClient(app), t["id"]).status_code == 303


def test_callback_needs_the_browser_that_started_the_login(idp_team):
    c, _, mails, t, idp = idp_team
    _invite(c, t, mails, "dev@example.com")
    r = _sso_login(idp, TestClient(app), t["id"], state="forged-state")
    assert r.status_code == 400
    victim = TestClient(app)  # login CSRF: a callback with no flow cookie at all
    assert victim.get("/sso/callback", params={"code": "abc", "state": "s"}, follow_redirects=False).status_code == 400
    assert victim.get("/sso/callback", params={"error": "access_denied"}, follow_redirects=False).status_code == 400
    assert victim.get("/me").status_code == 401


def test_sso_login_unknown_disabled_and_rate_limited(idp_team):
    c, mp, _, t, idp = idp_team
    assert c.get("/sso/ten_nope/login", follow_redirects=False).status_code == 404
    assert c.put("/sso", json={"enabled": False}, headers=_h(t)).json()["enabled"] is False
    assert c.get(f"/sso/{t['id']}/login", follow_redirects=False).status_code == 404
    c.put("/sso", json={"enabled": True}, headers=_h(t))
    mp.setenv("PRODUCT_SSO_PER_IP_HOUR", "2")
    codes = [c.get(f"/sso/{t['id']}/login", follow_redirects=False).status_code for _ in range(3)]
    assert codes[-1] == 429


def test_invitation_to_an_sso_team_does_not_sign_in_by_link(idp_team):
    c, _, mails, t, _ = idp_team
    _, r = _join(t, mails, "dev@example.com")
    assert r.status_code == 200 and r.json()["signed_in"] is False
    assert r.json()["sso_login"] == f"http://testserver/sso/{t['id']}/login"


def test_changing_the_idp_unbinds_subjects_but_keeping_it_does_not(idp_team):
    c, _, mails, t, idp = idp_team
    _join(t, mails, "dev@example.com")
    _sso_login(idp, TestClient(app), t["id"])
    assert c.get("/members", headers=_h(t)).json()["members"][-1]["sso_linked"] is True
    c.put("/sso", json={"issuer": ISSUER, "client_id": CLIENT_ID}, headers=_h(t))  # secret omitted, same IdP: kept
    assert c.get("/members", headers=_h(t)).json()["members"][-1]["sso_linked"] is True
    assert c.put("/sso", json={"issuer": ISSUER, "client_id": "client-2"}, headers=_h(t)).status_code == 422  # new client needs a secret
    c.put("/sso", json={"issuer": ISSUER, "client_id": "client-2", "client_secret": "new"}, headers=_h(t))
    assert c.get("/members", headers=_h(t)).json()["members"][-1]["sso_linked"] is False


def test_rotated_secret_key_fails_closed(idp_team):
    c, mp, mails, t, idp = idp_team
    _invite(c, t, mails, "dev@example.com")
    mp.setenv("PRODUCT_SECRET_KEY", "z" * 40)
    dev = TestClient(app)
    r = dev.get(f"/sso/{t['id']}/login", follow_redirects=False)
    assert r.status_code == 302
    idp.auth = {k: v[0] for k, v in parse_qs(urlparse(r.headers["location"]).query).items()}
    r = dev.get("/sso/callback", params={"code": "abc", "state": idp.auth["state"]}, follow_redirects=False)
    assert r.status_code == 400 and dev.get("/me").status_code == 401  # stored secret cannot be decrypted


def test_removing_the_sso_config_turns_login_off(idp_team):
    c, _, _, t, _ = idp_team
    assert c.delete("/sso", headers=_h(t)).json() == {"deleted": 1}
    assert c.get("/sso", headers=_h(t)).json()["configured"] is False
    assert c.get(f"/sso/{t['id']}/login", follow_redirects=False).status_code == 404


def test_logout_clears_the_session(team):
    c, _, mails = team
    t = _tenant(c)
    dev, _ = _join(t, mails, "dev@example.com")
    assert dev.get("/me").status_code == 200
    assert dev.post("/logout").json() == {"signed_out": True}
    assert dev.get("/me").status_code == 401


# ---- the tenant-supplied IdP URL is fetched by this server ------------------------------------------------

def test_validate_url_refuses_non_public_destinations(monkeypatch):
    monkeypatch.delenv("PRODUCT_SSO_ALLOW_HTTP", raising=False)
    answer = []
    monkeypatch.setattr(sso.socket, "getaddrinfo", lambda host, port, type=0: [(2, 1, 6, "", (a, 443)) for a in answer])
    for bad in ("http://idp.example.test", "ftp://idp.example.test", "file:///etc/passwd", "https://u:p@idp.example.test",
                "https:///nohost", "idp.example.test", None):
        with pytest.raises(sso.SSOError):
            sso.validate_url(bad)
    for addr in ("169.254.169.254", "10.0.0.5", "127.0.0.1", "192.168.1.1", "::1", "::ffff:10.0.0.1", "fd00::1"):
        answer[:] = [addr]
        with pytest.raises(sso.SSOError, match="non-public"):
            sso.validate_url("https://idp.example.test")
    answer[:] = ["93.184.216.34", "169.254.169.254"]  # one bad address among good ones is enough
    with pytest.raises(sso.SSOError, match="non-public"):
        sso.validate_url("https://idp.example.test")
    answer[:] = ["93.184.216.34"]
    assert sso.validate_url("https://idp.example.test/.well-known/x") == "https://idp.example.test/.well-known/x"


def test_idp_requests_do_not_follow_redirects_and_are_size_limited(monkeypatch):
    seen = {}

    class Resp:
        def __init__(self, status, chunks):
            self.status_code, self._c = status, chunks

        def iter_bytes(self):
            yield from self._c

    def stream_for(status, chunks):
        @contextmanager
        def fake(method, url, **kw):
            seen.update(kw)
            yield Resp(status, chunks)
        return fake

    monkeypatch.setenv("PRODUCT_SSO_ALLOW_HTTP", "1")
    monkeypatch.setattr(sso.httpx, "stream", stream_for(302, [b""]))
    with pytest.raises(sso.SSOError, match="HTTP 302"):
        sso.fetch_json("https://idp.test/x")
    assert seen["follow_redirects"] is False and seen["timeout"] <= 10
    monkeypatch.setattr(sso.httpx, "stream", stream_for(200, [b"x" * 600_000, b"x" * 600_000]))
    with pytest.raises(sso.SSOError, match="too large"):
        sso.fetch_json("https://idp.test/x")
    monkeypatch.setattr(sso.httpx, "stream", stream_for(200, [b'{"a":', b"1}"]))
    assert sso.fetch_json("https://idp.test/x") == {"a": 1}


def test_a_reclaimed_signup_email_takes_its_owner_seat_with_it(team):
    old = store.create_tenant("squatter", "free", "victim@example.com", terms_version="v1")
    store.start_verification(old["id"])  # a link went out and was never opened: the claim can be taken over
    assert [m["email"] for m in store.list_members(old["id"])] == ["victim@example.com"]
    new = store.create_tenant("victim co", "free", "victim@example.com", terms_version="v1", reclaim_after=0)
    assert store.list_members(old["id"]) == []  # the squatter keeps its keys but no seat tied to that address
    assert [(m["email"], m["role"]) for m in store.list_members(new["id"])] == [("victim@example.com", "owner")]


def test_seat_drift_ignores_plans_that_do_not_bill_seats(team):
    c, mp, _ = team
    mp.setenv("PRODUCT_PLANS", '{"starter": {"seat_cents": null}}')
    t = _tenant(c)
    store.update_tenant(t["id"], stripe_subscription_id="sub_1", subscription_status="active")
    assert store.seat_drift() == []
