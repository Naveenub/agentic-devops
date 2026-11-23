"""Members (seats), invitations and OIDC sign-in.

A tenant is billed per seat: a seat is an active member (owner included). API keys are not seats; a key acts as the
tenant's owner. Everything here needs the caller to be an API key or a signed-in member (auth.current_principal).

  GET    /me                      who the caller is: tenant, member, role, via
  GET    /members                 members and invitations, plus seat figures (billed, max, price)
  POST   /members {"email", "role": "member"|"admin"}   owners/admins: invite by email (202); needs outbound mail
  PATCH  /members/{id} {"role"}   owners/admins; only an owner can grant or change `owner`
  DELETE /members/{id}            owners/admins; frees the seat and ends that person's sessions at once
  GET    /invite                  opened from the emailed link (HTML, public); POST /invite/accept {"token"}
  GET    /sso    PUT /sso    DELETE /sso     owners/admins: the tenant's OIDC settings (see product/sso.py)
  GET    /sso/{tenant_id}/login   start sign-in (public);  GET /sso/callback   the IdP returns here (public)
  POST   /logout                  clear the session cookie

  PRODUCT_INVITES_PER_TENANT_HOUR  default 20     PRODUCT_INVITES_PER_DAY  default 2000 (whole deployment)
  PRODUCT_SSO_PER_IP_HOUR          default 60     PRODUCT_SSO_PER_DAY      default 5000 (whole deployment)

A change in member count is pushed to Stripe afterwards, in the background (subscriptions.sync_seats); if Stripe is
unreachable the change still stands and the billing scheduler re-syncs it."""
import html
import logging

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse

from product import mailer, sso, store, subscriptions
from product.admin import require_enabled
from product.auth import current_principal, require_manager
from product.plans import get_plan
from product.web import _client_ip, _int_env, _page

log = logging.getLogger("product.team")
team_router = APIRouter(dependencies=[Depends(require_enabled)])


def _cap(tenant: dict) -> int | None:
    return get_plan(tenant["plan"]).max_seats


def _seats(tenant: dict) -> dict:
    plan = get_plan(tenant["plan"])
    return {"billed": store.seat_count(tenant["id"]), "max": plan.max_seats, "price_cents": plan.seat_cents,
            "currency": plan.currency}


def _limit(tenant: dict, n) -> HTTPException:
    return HTTPException(status_code=402, detail=f"plan '{tenant['plan']}' allows {n} member{'s' if n != 1 else ''}; "
                                                 "upgrade to add more")


def _cookie(resp, name: str, value: str, max_age: int, path: str = "/") -> None:
    resp.set_cookie(name, value, max_age=max_age, httponly=True, secure=sso.secure_cookies(), samesite="lax", path=path)


# ---- who am I, members ---------------------------------------------------------------------------------

@team_router.get("/me")
def me(p: dict = Depends(current_principal)):
    t = p["tenant"]
    return {"tenant": {"id": t["id"], "name": t["name"], "plan": t["plan"]}, "member": p["member"], "role": p["role"],
            "via": p["via"]}


@team_router.get("/members")
def members(p: dict = Depends(current_principal)):
    return {"members": store.list_members(p["tenant"]["id"]), "seats": _seats(p["tenant"])}


def _send_invite(email: str, token: str, team: str) -> None:
    link = f"{mailer.public_url()}/invite#t={token}"  # fragment: not sent to a server, so not logged
    mailer.send_quietly(
        email, "You have been invited to DevOps AI Agent",
        f"You have been invited to join the team \"{team}\" on DevOps AI Agent.\n\n"
        f"Open this link within {store.INVITE_TTL // 86400} days to accept:\n{link}\n\n"
        "If you do not know this team, ignore this email: nothing happens unless the link is opened.\n")


@team_router.post("/members", status_code=202)
def invite(payload: dict, background: BackgroundTasks, p: dict = Depends(require_manager)):
    if not mailer.configured():
        raise HTTPException(status_code=503, detail="email is not set up on this server, so invitations cannot be sent")
    t = p["tenant"]
    try:
        email = store.clean_email(payload.get("email"))
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))
    role = payload.get("role", "member")
    if role not in ("member", "admin"):
        raise HTTPException(status_code=422, detail="'role' must be 'member' or 'admin'")
    if not store.invite_hit(t["id"], _int_env("PRODUCT_INVITES_PER_TENANT_HOUR", 20),
                            _int_env("PRODUCT_INVITES_PER_DAY", 2000)):
        raise HTTPException(status_code=429, detail="too many invitations, try again later", headers={"Retry-After": "3600"})
    try:
        member, token = store.invite_member(t["id"], email, role, _cap(t))
    except store.SeatLimit as e:
        raise _limit(t, e.args[0])
    except store.MemberError as e:
        raise HTTPException(status_code=409, detail=str(e))
    background.add_task(_send_invite, email, token, t["name"])
    return {"member": member}


def _target(p: dict, mid: str) -> dict:
    m = store.get_member(p["tenant"]["id"], mid)
    if not m or m["status"] == "removed":
        raise HTTPException(status_code=404, detail="no such member")
    return m


@team_router.patch("/members/{mid}")
def change_role(mid: str, payload: dict, p: dict = Depends(require_manager)):
    m, role = _target(p, mid), payload.get("role")
    if role not in store.ROLES:
        raise HTTPException(status_code=422, detail="'role' must be one of owner, admin, member")
    if "owner" in (m["role"], role) and p["role"] != "owner":
        raise HTTPException(status_code=403, detail="only an owner can change owners")
    try:
        return {"member": store.set_member_role(p["tenant"]["id"], mid, role)}
    except store.MemberError as e:
        raise HTTPException(status_code=409, detail=str(e))


@team_router.delete("/members/{mid}")
def remove(mid: str, background: BackgroundTasks, p: dict = Depends(require_manager)):
    m = _target(p, mid)
    if m["role"] == "owner" and p["role"] != "owner":
        raise HTTPException(status_code=403, detail="only an owner can remove an owner")
    try:
        store.remove_member(p["tenant"]["id"], mid)
    except store.MemberError as e:
        raise HTTPException(status_code=409, detail=str(e))
    if m["status"] == "active":
        background.add_task(subscriptions.sync_seats_quietly, p["tenant"]["id"])
    return {"removed": mid}


# ---- accepting an invitation ---------------------------------------------------------------------------

@team_router.post("/invite/accept")
def invite_accept(payload: dict, background: BackgroundTasks):
    """Public; the one-time token is the credential. Opening the mailed link proves the mailbox, so the member
    becomes active (a seat) and, unless the team requires SSO, is signed in."""
    token = payload.get("token")
    try:
        m = store.redeem_invite(token, _cap) if isinstance(token, str) and 16 <= len(token) <= 200 else None
    except store.SeatLimit as e:
        raise HTTPException(status_code=402, detail=f"this team's plan allows {e.args[0]} members; ask an admin to upgrade")
    if not m:
        raise HTTPException(status_code=400, detail="this invitation is invalid, expired or already used")
    tid = m["tenant_id"]
    t = store.get_tenant(tid)
    background.add_task(subscriptions.sync_seats_quietly, tid)
    sso_cfg = store.get_sso(tid)
    signed_in = sso.configured() and not (sso_cfg and sso_cfg["enabled"])  # an SSO team signs in through its IdP
    resp = JSONResponse({"member": m, "team": t["name"], "signed_in": signed_in,
                         "sso_login": f"{mailer.public_url()}/sso/{tid}/login" if sso_cfg and sso_cfg["enabled"] else None})
    if signed_in:
        _cookie(resp, sso.SESSION_COOKIE, sso.make_session(tid, m["id"]), sso.session_ttl())
    return resp


@team_router.get("/invite")
def invite_page():
    body = ('<h1>Join your team</h1><div id="ask" class="box"><p class="lead">Open the link we emailed you.</p></div>'
            '<div id="go" class="box" hidden><p class="lead">Accept the invitation. This uses one seat on the team\'s plan.</p>'
            '<div class="row"><button id="b" type="button">Accept invitation</button></div></div>'
            '<div id="msg" class="msg box" role="status"></div>')
    script = """
const $=id=>document.getElementById(id);
const say=(t,c)=>{$('msg').className='msg box '+(c||'');$('msg').textContent=t};
const tok=new URLSearchParams(location.hash.slice(1)).get('t')||'';
if(tok){history.replaceState(null,'',location.pathname);$('ask').hidden=true;$('go').hidden=false}
$('b').addEventListener('click',async()=>{
  say('Accepting…');
  try{
    const r=await fetch('/invite/accept',{method:'POST',headers:{'content-type':'application/json'},body:JSON.stringify({token:tok})});
    const b=await r.json().catch(()=>({}));
    if(!r.ok)throw new Error(typeof b.detail==='string'?b.detail:'Request failed ('+r.status+')');
    $('go').hidden=true;
    if(b.signed_in){location.href='/account'}
    else if(b.sso_login){say('You have joined '+b.team+'. Sign in with your company login to continue.','ok');$('msg').appendChild(document.createElement('br'));const a=document.createElement('a');a.href=b.sso_login;a.textContent='Sign in with SSO';$('msg').appendChild(a)}
    else say('You have joined '+b.team+'.','ok');
  }catch(e){say(e.message,'err')}
});
"""
    return _page("Join your team", body, script, cache=False)


# ---- SSO settings (owners and admins) --------------------------------------------------------------------

def _sso_view(tid: str) -> dict:
    cfg = store.get_sso(tid)
    base = mailer.public_url()
    return {"configured": bool(cfg), "issuer": cfg and cfg["issuer"], "client_id": cfg and cfg["client_id"],
            "enabled": bool(cfg and cfg["enabled"]),
            "redirect_uri": sso.redirect_uri() if sso.configured() else None,  # register this at the IdP
            "login_url": f"{base}/sso/{tid}/login" if cfg and base else None}


@team_router.get("/sso")
def sso_get(p: dict = Depends(require_manager)):
    return _sso_view(p["tenant"]["id"])


def _text(payload: dict, field: str, limit: int) -> str | None:
    v = payload.get(field)
    if v is None:
        return None
    if not isinstance(v, str) or not v.strip() or len(v) > limit:
        raise HTTPException(status_code=422, detail=f"'{field}' must be a non-empty string of at most {limit} characters")
    return v.strip()


@team_router.put("/sso")
def sso_put(payload: dict, p: dict = Depends(require_manager)):
    """Set the tenant's IdP: {"issuer", "client_id", "client_secret", "enabled"}. Discovery is fetched now, so a
    wrong issuer fails here with a reason. client_secret may be left out to keep the stored one while neither
    issuer nor client_id changes; with only {"enabled": bool} the sign-in is switched on or off."""
    t = p["tenant"]
    if not get_plan(t["plan"]).sso:
        raise HTTPException(status_code=402, detail=f"SSO is not included in plan '{t['plan']}'")
    if not sso.configured():
        raise HTTPException(status_code=503, detail="SSO is not set up on this server "
                                                    "(PRODUCT_SECRET_KEY, PRODUCT_PUBLIC_URL, PyJWT)")
    enabled = payload.get("enabled", True)
    if not isinstance(enabled, bool):
        raise HTTPException(status_code=422, detail="'enabled' must be true or false")
    existing = store.get_sso(t["id"])
    issuer, client_id, secret = _text(payload, "issuer", 2048), _text(payload, "client_id", 512), _text(payload, "client_secret", 2048)
    if not (issuer or client_id or secret):
        if not existing or "enabled" not in payload:
            raise HTTPException(status_code=422, detail="'issuer', 'client_id' and 'client_secret' are required")
        store.set_sso_enabled(t["id"], enabled)
        return _sso_view(t["id"])
    issuer, client_id = issuer or (existing and existing["issuer"]), client_id or (existing and existing["client_id"])
    if not (issuer and client_id):
        raise HTTPException(status_code=422, detail="'issuer' and 'client_id' are required")
    same_idp = bool(existing) and (existing["issuer"], existing["client_id"]) == (issuer, client_id)
    if not secret and not same_idp:
        raise HTTPException(status_code=422, detail="'client_secret' is required when the issuer or client changes")
    try:
        sso.check_config(issuer)
        enc = sso.encrypt_secret(secret) if secret else existing["client_secret_enc"]
    except sso.SSOError as e:
        raise HTTPException(status_code=422, detail=str(e))
    store.put_sso(t["id"], issuer, client_id, enc, enabled)
    return _sso_view(t["id"])


@team_router.delete("/sso")
def sso_delete(p: dict = Depends(require_manager)):
    return {"deleted": store.delete_sso(p["tenant"]["id"])}


# ---- sign-in ---------------------------------------------------------------------------------------------

def _fail(message: str, status: int = 400):
    r = _page("Sign-in failed", f'<h1>Sign-in failed</h1><p class="lead">{html.escape(message)}</p>', cache=False)
    r.status_code = status
    r.delete_cookie(sso.FLOW_COOKIE, path="/sso")
    return r


@team_router.get("/sso/callback")
def sso_callback(request: Request, background: BackgroundTasks, code: str | None = None, state: str | None = None,
                 error: str | None = None):
    if not sso.configured():
        return _fail("Single sign-on is not set up on this server.", 503)
    if error or not code or not state:
        return _fail("The identity provider did not complete the sign-in.")
    try:
        tid, claims = sso.finish(request.cookies.get(sso.FLOW_COOKIE), code, state)
    except sso.SSOError as e:
        log.warning("sso sign-in failed: %s", e)  # the reason is for the operator; the browser gets a generic page
        return _fail("The sign-in could not be completed. Start again from your team's sign-in link.")
    email = None
    if claims.get("email_verified") not in (False, "false"):  # Entra omits the claim; an explicit false disables the email match
        try:
            email = store.clean_email(claims.get("email"))
        except ValueError:
            pass
    tenant = store.get_tenant(tid)
    before = store.active_members(tid)
    try:
        member = store.sso_member(tid, claims["sub"], email, _cap) if tenant and tenant["active"] else None
    except store.SeatLimit as e:
        return _fail(f"This team's plan allows {e.args[0]} members. Ask a team admin to upgrade.", 402)
    if not member:
        return _fail("This login is not a member of that team. Ask a team admin to invite your work email.", 403)
    if store.active_members(tid) != before:
        background.add_task(subscriptions.sync_seats_quietly, tid)
    resp = RedirectResponse("/account", status_code=303)
    resp.delete_cookie(sso.FLOW_COOKIE, path="/sso")
    _cookie(resp, sso.SESSION_COOKIE, sso.make_session(tid, member["id"]), sso.session_ttl())
    return resp


@team_router.get("/sso/{tid}/login")
def sso_login(tid: str, request: Request):
    if not sso.configured():
        return _fail("Single sign-on is not set up on this server.", 503)
    if not store.sso_hit(_client_ip(request), _int_env("PRODUCT_SSO_PER_IP_HOUR", 60), _int_env("PRODUCT_SSO_PER_DAY", 5000)):
        r = _fail("Too many sign-in attempts. Try again later.", 429)
        r.headers["Retry-After"] = "3600"
        return r
    try:
        url, flow = sso.begin(tid)
    except sso.SSOError as e:
        log.info("sso login for %s refused: %s", tid, e)  # same page whether the tenant exists or not
        return _fail("Single sign-on is not available for that team.", 404)
    resp = RedirectResponse(url, status_code=302)
    _cookie(resp, sso.FLOW_COOKIE, flow, sso.FLOW_TTL, path="/sso")
    return resp


@team_router.post("/logout")
def logout():
    resp = JSONResponse({"signed_out": True})
    resp.delete_cookie(sso.SESSION_COOKIE, path="/")
    return resp
