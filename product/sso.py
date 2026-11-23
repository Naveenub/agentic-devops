"""Per-tenant OpenID Connect sign-in (Google, Okta, Entra, any compliant IdP) and the signed session cookie.

A tenant admin registers its own IdP with PUT /sso {issuer, client_id, client_secret}. Members then sign in at
GET /sso/{tenant_id}/login. Flow: authorization code + PKCE (S256) + state + nonce, one fixed redirect URI
(`<PRODUCT_PUBLIC_URL>/sso/callback`, register it at the IdP). The ID token is verified here: signature against the
IdP's JWKS (RS256/ES256 only), `iss`, `aud`, `exp`, `iat`, `nonce`. Nobody is created by a login: the person must
already be a member (invited) of that tenant, matched by the IdP subject once bound, or by email the first time
(store.sso_member). `email_verified: false` disables the email match; Entra omits the claim, which is accepted
because the IdP is the customer's own, fixed per tenant.

  PRODUCT_SECRET_KEY    at least 32 characters. Signs session and flow cookies and encrypts stored client secrets.
                        Unset or short = SSO and sessions are off. Rotating it signs everyone out and makes stored
                        client secrets unreadable, so tenants must PUT /sso again.
  PRODUCT_PUBLIC_URL    already used for emailed links; the redirect URI and the cookies' Secure flag come from it
  PRODUCT_SESSION_TTL   seconds a session cookie lives, default 28800 (8 hours)
  PRODUCT_SSO_ALLOW_HTTP=1   development only: allow http:// and private addresses for the IdP

The IdP URL is tenant-supplied, so this server fetches it: every URL (issuer, discovery endpoints) must be https and
resolve only to public addresses, redirects are not followed, replies are size-limited. DNS rebinding between the
check and the connection is not closed here; keep outbound traffic from this host firewalled from internal ranges.

Sessions are signed (not stored) cookies, re-checked against tenant_members on every request, so removing a member
ends their access at once; there is no way to end one session without removing the member."""
import base64
import hashlib
import hmac
import ipaddress
import json
import logging
import os
import secrets
import socket
import time
from urllib.parse import urlencode, urlparse

import httpx

from product import mailer, store

log = logging.getLogger("product.sso")

SESSION_COOKIE = "adk_session"
FLOW_COOKIE = "adk_sso"
FLOW_TTL = 600
MAX_BODY = 1_000_000
ALGS = ("RS256", "ES256")


class SSOError(Exception):
    """Something about the sign-in failed. The message is for the operator's log and the admin's PUT /sso reply;
    the browser only ever sees a generic failure."""


# ---- keys, cookies, encrypted client secrets -------------------------------------------------------

def _key() -> bytes | None:
    k = os.getenv("PRODUCT_SECRET_KEY", "")
    return k.encode() if len(k) >= 32 else None


def _jwt():
    try:
        import jwt  # PyJWT with the crypto extra: pip install -r product/requirements.txt
        return jwt
    except ImportError:
        return None


def configured() -> bool:
    return bool(_key() and mailer.public_url().startswith(("http://", "https://")) and _jwt())


def _derive(purpose: str) -> bytes:
    return hmac.new(_key() or b"", b"adk:" + purpose.encode(), hashlib.sha256).digest()


def _b64(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def _unb64(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def sign(payload: dict, purpose: str) -> str:
    body = _b64(json.dumps(payload, separators=(",", ":")).encode())
    return f"{body}.{_b64(hmac.new(_derive(purpose), body.encode(), hashlib.sha256).digest())}"


def unsign(token: str | None, purpose: str) -> dict | None:
    """The payload of a token made by `sign` for the same purpose, or None if forged, malformed or expired."""
    if not token or not _key():
        return None
    try:
        body, mac = token.split(".")
        want = _b64(hmac.new(_derive(purpose), body.encode(), hashlib.sha256).digest())
        if not hmac.compare_digest(mac, want):
            return None
        payload = json.loads(_unb64(body))
        return payload if isinstance(payload, dict) and payload.get("exp", 0) > time.time() else None
    except (ValueError, TypeError):
        return None


def _fernet():
    from cryptography.fernet import Fernet
    return Fernet(base64.urlsafe_b64encode(_derive("client-secret")))


def encrypt_secret(plain: str) -> str:
    return _fernet().encrypt(plain.encode()).decode()


def decrypt_secret(token: str) -> str:
    from cryptography.fernet import InvalidToken
    try:
        return _fernet().decrypt(token.encode()).decode()
    except InvalidToken:
        raise SSOError("the stored client secret cannot be decrypted (PRODUCT_SECRET_KEY changed?); PUT /sso again")


def session_ttl() -> int:
    try:
        return max(60, int(os.getenv("PRODUCT_SESSION_TTL", "28800")))
    except ValueError:
        return 28800


def make_session(tid: str, mid: str) -> str:
    return sign({"t": tid, "m": mid, "exp": time.time() + session_ttl()}, "session")


def secure_cookies() -> bool:
    return mailer.public_url().startswith("https://")


# ---- outbound requests to a tenant-supplied IdP -----------------------------------------------------

def validate_url(url: str, resolve: bool = True) -> str:
    """https only, no credentials in the URL, and (resolve=True) every address the host resolves to is public.
    Returns the URL. Raises SSOError."""
    dev = os.getenv("PRODUCT_SSO_ALLOW_HTTP") == "1"
    u = urlparse(url if isinstance(url, str) else "")
    if u.scheme != "https" and not (dev and u.scheme == "http"):
        raise SSOError("the URL must start with https://")
    if not u.hostname or u.username or u.password or len(url) > 2048:
        raise SSOError("the URL is not valid")
    if resolve and not dev:
        try:
            infos = socket.getaddrinfo(u.hostname, u.port or 443, type=socket.SOCK_STREAM)
        except OSError:
            raise SSOError(f"cannot resolve {u.hostname}")
        for info in infos:
            ip = ipaddress.ip_address(info[4][0])
            ip = getattr(ip, "ipv4_mapped", None) or ip
            if not ip.is_global:
                raise SSOError("the URL resolves to a non-public address")
    return url


def _request(method: str, url: str, data: dict | None = None):
    validate_url(url)
    try:
        with httpx.stream(method, url, data=data, timeout=10, follow_redirects=False,
                          headers={"Accept": "application/json"}) as r:
            body = bytearray()
            for chunk in r.iter_bytes():
                body += chunk
                if len(body) > MAX_BODY:
                    raise SSOError("the IdP's reply is too large")
            status = r.status_code
    except httpx.HTTPError as e:
        raise SSOError(f"the IdP is unreachable ({type(e).__name__})")
    if status != 200:
        raise SSOError(f"the IdP answered HTTP {status}")
    try:
        return json.loads(bytes(body))
    except ValueError:
        raise SSOError("the IdP's reply is not JSON")


def fetch_json(url: str):
    return _request("GET", url)


def post_form(url: str, data: dict):
    return _request("POST", url, data)


_disco: dict = {}  # issuer -> (expires, document)
_jwks: dict = {}  # jwks_uri -> (fetched_at, keys)


def discovery(issuer: str) -> dict:
    """The IdP's OpenID configuration. Its `issuer` must equal what the tenant configured, exactly, and every
    endpoint must be https."""
    hit = _disco.get(issuer)
    if hit and hit[0] > time.time():
        return hit[1]
    doc = fetch_json(issuer.rstrip("/") + "/.well-known/openid-configuration")
    if not isinstance(doc, dict) or doc.get("issuer") != issuer:
        raise SSOError("the discovery document's issuer does not match the configured issuer (Entra: use the "
                       "tenant-specific https://login.microsoftonline.com/<tenant-id>/v2.0, not /common)")
    for k in ("authorization_endpoint", "token_endpoint", "jwks_uri"):
        validate_url(doc.get(k), resolve=(k != "authorization_endpoint"))  # the browser, not this server, visits that one
    _disco[issuer] = (time.time() + 3600, doc)
    return doc


def _signing_key(jwks_uri: str, kid: str | None):
    """The IdP's public key for `kid`. Keys are cached; an unknown kid refetches the set, but at most once a minute,
    because the kid comes from the token and so from whoever is signing in."""
    jwt = _jwt()

    def find(keys):
        sig = [k for k in keys if k.get("use", "sig") == "sig"]
        match = [k for k in sig if k.get("kid") == kid] if kid else (sig if len(sig) == 1 else [])
        return jwt.PyJWK(match[0]).key if match else None

    hit = _jwks.get(jwks_uri)
    if hit:
        key = find(hit[1])
        if key is not None:
            return key
        if time.time() - hit[0] < 60:
            raise SSOError("the ID token's signing key is not in the IdP's JWKS")
    keys = fetch_json(jwks_uri).get("keys", [])
    _jwks[jwks_uri] = (time.time(), keys)
    key = find(keys)
    if key is None:
        raise SSOError("the ID token's signing key is not in the IdP's JWKS")
    return key


def verify_id_token(id_token, cfg: dict, doc: dict, nonce: str) -> dict:
    jwt = _jwt()
    if not isinstance(id_token, str):
        raise SSOError("no id_token in the token response")
    try:
        header = jwt.get_unverified_header(id_token)
        alg = header.get("alg")
        if alg not in ALGS:
            raise SSOError(f"unsupported ID token algorithm {alg!r}")
        key = _signing_key(doc["jwks_uri"], header.get("kid"))
        issuers = [cfg["issuer"]] + (["accounts.google.com"] if cfg["issuer"] == "https://accounts.google.com" else [])
        claims = jwt.decode(id_token, key, algorithms=[alg], audience=cfg["client_id"], issuer=issuers, leeway=60,
                            options={"require": ["exp", "iat", "iss", "aud", "sub"]})
    except jwt.PyJWTError as e:
        raise SSOError(f"the ID token was rejected: {type(e).__name__}: {e}")
    if not isinstance(claims.get("sub"), str) or not claims["sub"]:
        raise SSOError("the ID token has no subject")
    if not isinstance(claims.get("nonce"), str) or not hmac.compare_digest(claims["nonce"], nonce):
        raise SSOError("the ID token's nonce does not match this sign-in")
    return claims


# ---- the flow -----------------------------------------------------------------------------------------

def redirect_uri() -> str:
    return mailer.public_url() + "/sso/callback"


def check_config(issuer: str) -> dict:
    """Used when an admin saves the configuration: fail early, with a reason, if discovery does not work."""
    validate_url(issuer)
    return discovery(issuer)


def begin(tid: str) -> tuple[str, str]:
    """(URL to send the browser to, value of the flow cookie)."""
    cfg = store.get_sso(tid)
    if not cfg or not cfg["enabled"]:
        raise SSOError("SSO is not enabled for this tenant")
    doc = discovery(cfg["issuer"])
    state, nonce, verifier = secrets.token_urlsafe(24), secrets.token_urlsafe(24), secrets.token_urlsafe(48)
    q = urlencode({"response_type": "code", "client_id": cfg["client_id"], "redirect_uri": redirect_uri(),
                   "scope": "openid email profile", "state": state, "nonce": nonce,
                   "code_challenge": _b64(hashlib.sha256(verifier.encode()).digest()), "code_challenge_method": "S256"})
    ep = doc["authorization_endpoint"]
    flow = sign({"t": tid, "s": state, "n": nonce, "v": verifier, "exp": time.time() + FLOW_TTL}, "flow")
    return f"{ep}{'&' if '?' in ep else '?'}{q}", flow


def finish(flow_cookie: str | None, code: str, state: str) -> tuple[str, dict]:
    """Check the callback against the flow cookie, redeem the code, verify the ID token. Returns (tenant id, claims)."""
    flow = unsign(flow_cookie, "flow")
    if not flow or not hmac.compare_digest(str(flow.get("s")), state):
        raise SSOError("the sign-in was not started in this browser, or it expired")
    cfg = store.get_sso(flow["t"])
    if not cfg or not cfg["enabled"]:
        raise SSOError("SSO is not enabled for this tenant")
    doc = discovery(cfg["issuer"])
    tok = post_form(doc["token_endpoint"], {
        "grant_type": "authorization_code", "code": code, "redirect_uri": redirect_uri(), "client_id": cfg["client_id"],
        "client_secret": decrypt_secret(cfg["client_secret_enc"]), "code_verifier": flow["v"]})
    return flow["t"], verify_id_token(tok.get("id_token") if isinstance(tok, dict) else None, cfg, doc, flow["n"])
