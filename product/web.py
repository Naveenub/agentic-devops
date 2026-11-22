"""Customer-facing layer: public pricing page, self-serve signup and an account/billing page.

  GET  /pricing    plans, included usage, prices (from plans.py), what is purchasable (HTML, public)
  GET  /signup     signup form (HTML, public)
  POST /signup     {"name": "...", "email": "..."} -> tenant on `free` + API key (once)
  GET  /recover    lost key: ask for an emailed link, or (from the link) create a new key (HTML, public)
  POST /recover    {"email": "..."} -> 202 whether or not the address is registered; a one-time link is emailed if it is
  POST /recover/confirm {"token": "..."} -> new API key (once); every older key of that tenant is revoked
  GET  /terms /privacy /dpa /refunds   the legal documents (HTML, public; 503 until product/legal_docs/entity.json is
                    complete and COUNSEL_REVIEWED is true, see product/legal.py)
  GET  /account    paste your API key: usage, upgrade (Stripe Checkout) and "Manage billing" (Stripe portal)
  GET  /billing    JSON state the account page renders (tenant auth, X-API-Key)

Paying is the existing flow: /account calls POST /billing/checkout and POST /billing/portal.

  PRODUCT_SIGNUP=on             open public signup. Also needs the legal documents published (python -m product.legal check)
                                and every signup must accept them. Closed unless set: every free account can spend the server's LLM
                                tokens, so opening it is the operator's call. (POST /signup -> 403 while closed;
                                operators still use POST /admin/tenants.)
  PRODUCT_SIGNUP_PER_IP_HOUR    default 5     PRODUCT_SIGNUP_PER_DAY   default 200 (whole deployment)
  PRODUCT_RECOVER_PER_IP_HOUR   default 5     PRODUCT_RECOVER_PER_DAY  default 200 (whole deployment)
  PRODUCT_TRUST_PROXY=1         take the client IP from the last X-Forwarded-For entry (set only behind a proxy
                                that appends it, else callers can spoof their IP and dodge the per-IP limits)
  PRODUCT_CONTACT_URL           where plans that cannot be bought online (e.g. enterprise) send people
  PRODUCT_PLANS                 prices come from plans.py (base_cents / overage per plan); overriding them here means
                                re-running `python -m product.stripe_sync --apply` so Stripe matches
Key recovery needs outbound mail (PRODUCT_SMTP_*, PRODUCT_PUBLIC_URL; see mailer.py); without it /recover answers 503."""
import html
import json
import os
import secrets

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse

from product import legal, mailer, store, subscriptions
from product.admin import require_enabled
from product.auth import current_tenant
from product.plans import _load

web_router = APIRouter(dependencies=[Depends(require_enabled)])

LABELS = {"analyze": "Analyze runs", "cost_check": "Cost checks", "llm_tokens": "LLM tokens"}
INLINE = {"analyze": "analyze runs", "cost_check": "cost checks", "llm_tokens": "LLM tokens"}
ORDER = ("free", "starter", "pro", "enterprise")


def _int_env(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, default))
    except ValueError:
        return default


def _ordered_plans() -> list:
    plans = _load()
    names = [n for n in ORDER if n in plans] + sorted(n for n in plans if n not in ORDER)
    return [plans[n] for n in names]


def signup_open() -> bool:
    """Closed unless the operator opts in (see module docstring) AND the legal documents are published: nobody
    should accept terms that still have blanks, or that counsel has not signed off."""
    return (os.getenv("PRODUCT_SIGNUP") or "").lower() in ("on", "1", "true", "yes", "open") and legal.configured()


def _client_ip(request: Request) -> str:
    if os.getenv("PRODUCT_TRUST_PROXY") == "1":
        fwd = request.headers.get("x-forwarded-for", "").split(",")[-1].strip()
        if fwd:
            return fwd
    return request.client.host if request.client else "unknown"


# ---- JSON ---------------------------------------------------------------------------------------

@web_router.post("/signup")
def signup(payload: dict, request: Request):
    """Public, and only while the operator has opened signup. Always creates a `free` tenant (paid plans go
    through Checkout, after the webhook confirms). The email is required: it is the only way to recover a lost key.
    No Stripe call here, so anonymous traffic cannot create Stripe customers."""
    if not signup_open():
        raise HTTPException(status_code=403, detail="signup is closed")
    if payload.get("accept_terms") is not True:
        raise HTTPException(status_code=422, detail="'accept_terms' must be true: you must accept the Terms of Service "
                                                    "and Privacy Policy (/terms, /privacy)")
    name = payload.get("name")
    if not isinstance(name, str) or not name.strip():
        raise HTTPException(status_code=422, detail="'name' is required")
    name = " ".join(name.split())
    if len(name) > 100:
        raise HTTPException(status_code=422, detail="'name' must be 100 characters or fewer")
    try:
        email = store.clean_email(payload.get("email"))
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))
    if not store.signup_hit(_client_ip(request), _int_env("PRODUCT_SIGNUP_PER_IP_HOUR", 5),
                            _int_env("PRODUCT_SIGNUP_PER_DAY", 200)):
        raise HTTPException(status_code=429, detail="too many signups, try again later", headers={"Retry-After": "3600"})
    try:
        t = store.create_tenant(name, "free", email, terms_version=legal.VERSION)
    except store.EmailTaken:
        raise HTTPException(status_code=409, detail="an account with that email already exists; " + (
            "use key recovery to get a new key" if mailer.configured() else "ask the operator for a new key"))
    return {**t, "api_key": store.issue_key(t["id"])}  # shown once


RECOVERY_ACK = (f"If that email is registered, a recovery link is on its way. "
                f"It is valid for {store.RECOVERY_TTL // 60} minutes and works once.")


def _send_recovery(email: str, token: str) -> None:
    link = f"{mailer.public_url()}/recover#t={token}"  # fragment: never sent to a server, so never logged or leaked in Referer
    mailer.send_quietly(
        email, "Recover your DevOps AI Agent API key",
        "Someone asked to recover the API key for the account registered to this address.\n\n"
        f"Open this link within {store.RECOVERY_TTL // 60} minutes to create a new key:\n{link}\n\n"
        "The link works once. Creating a new key revokes every existing key on the account.\n"
        "If you did not ask for this, ignore this email: nothing changes unless the link is opened.\n")


@web_router.post("/recover", status_code=202)
def recover(payload: dict, request: Request, background: BackgroundTasks):
    """Public. Identical answer whether or not the address is registered (and the mail goes out after the
    response), so it cannot be used to find out who has an account."""
    if not mailer.configured():
        raise HTTPException(status_code=503, detail="key recovery by email is not set up; ask the operator for a new key")
    try:
        email = store.clean_email(payload.get("email"))
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))
    if not store.recover_hit(_client_ip(request), _int_env("PRODUCT_RECOVER_PER_IP_HOUR", 5),
                             _int_env("PRODUCT_RECOVER_PER_DAY", 200)):
        raise HTTPException(status_code=429, detail="too many recovery requests, try again later", headers={"Retry-After": "3600"})
    t = store.tenant_for_email(email)
    token = store.start_recovery(t["id"]) if t and t["active"] else None
    if token:
        background.add_task(_send_recovery, email, token)
    return {"status": RECOVERY_ACK}


@web_router.post("/recover/confirm")
def recover_confirm(payload: dict):
    """Public; the one-time token is the credential. Revokes the tenant's keys and returns a new one (once)."""
    token = payload.get("token")
    got = store.redeem_recovery(token) if isinstance(token, str) and 16 <= len(token) <= 200 else None
    if not got:
        raise HTTPException(status_code=400, detail="this recovery link is invalid, expired or already used")
    t, key = got
    return {"id": t["id"], "name": t["name"], "plan": t["plan"], "api_key": key}  # shown once


@web_router.get("/billing")
def billing_state(tenant: dict = Depends(current_tenant)):
    """What /account needs: the plan, the subscription state, and which paid plans can be bought right now."""
    has_sub = bool(tenant.get("stripe_subscription_id")) and tenant.get("subscription_status") not in subscriptions.DEAD
    return {
        "tenant": tenant["id"], "name": tenant["name"], "plan": tenant["plan"],
        "subscription_status": tenant.get("subscription_status"),
        "has_subscription": has_sub,
        "can_manage": bool(tenant.get("stripe_customer_id")),  # the portal needs a Stripe customer
        "purchasable": [p.name for p in _ordered_plans() if subscriptions.purchasable(p.name)] if not has_sub else [],
    }


# ---- HTML ---------------------------------------------------------------------------------------

CSS = """
:root{--bg:#fafaf9;--fg:#1c1917;--mut:#57534e;--card:#fff;--line:#e7e5e4;--acc:#4338ca;--accfg:#fff;--bad:#b91c1c;--ok:#15803d}
@media(prefers-color-scheme:dark){:root{--bg:#0c0a09;--fg:#f5f5f4;--mut:#a8a29e;--card:#1c1917;--line:#292524;--acc:#818cf8;--accfg:#0c0a09;--bad:#f87171;--ok:#4ade80}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);font:16px/1.5 system-ui,-apple-system,Segoe UI,sans-serif}
header,main,footer{max-width:1000px;margin:0 auto;padding:0 16px}footer{display:flex;gap:18px;flex-wrap:wrap;padding-top:32px;padding-bottom:28px;font-size:.88rem}header{display:flex;gap:20px;align-items:center;padding-top:20px;padding-bottom:12px}
header b{margin-right:auto}a{color:var(--acc)}h1{font-size:1.9rem;margin:28px 0 6px}p.lead{color:var(--mut);margin:0 0 24px}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(210px,1fr));gap:14px}
.card{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:20px;display:flex;flex-direction:column}
.card h2{margin:0;font-size:1.1rem;text-transform:capitalize}.price{font-size:1.6rem;font-weight:650;margin:6px 0 2px;min-height:2.4rem}
.card ul{list-style:none;padding:0;margin:12px 0 18px;color:var(--mut);font-size:.93rem;flex:1}.card li{padding:3px 0}
.btn,button{display:inline-block;font:inherit;background:var(--acc);color:var(--accfg);border:0;border-radius:8px;padding:9px 16px;cursor:pointer;text-decoration:none;text-align:center}
.btn.alt,button.alt{background:transparent;color:var(--acc);border:1px solid var(--acc)}.btn[aria-disabled=true]{opacity:.5;pointer-events:none}
input{font:inherit;width:100%;padding:9px 11px;border:1px solid var(--line);border-radius:8px;background:var(--card);color:var(--fg)}
label{display:block;font-weight:600;margin:14px 0 6px}.box{max-width:520px}.row{display:flex;gap:10px;flex-wrap:wrap;margin-top:16px}
code{background:var(--card);border:1px solid var(--line);border-radius:6px;padding:2px 6px;word-break:break-all}
label.chk{display:flex;gap:10px;align-items:flex-start;font-weight:400;margin-top:16px}label.chk input{width:auto;margin-top:5px}.msg{margin-top:14px;min-height:1.4em}.err{color:var(--bad)}.ok{color:var(--ok)}
table{border-collapse:collapse;width:100%;margin-top:10px}td,th{text-align:left;padding:7px 4px;border-bottom:1px solid var(--line)}
.fine{color:var(--mut);font-size:.88rem;margin-top:22px}.fine.tight{margin-top:6px}.price .note{font-size:1rem;font-weight:400;color:var(--mut)}[hidden]{display:none!important}
"""


def _page(title: str, body: str, script: str = "", cache: bool = True) -> HTMLResponse:
    nonce = secrets.token_urlsafe(16)
    doc = (f'<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">'
           f'<title>{html.escape(title)}</title><style nonce="{nonce}">{CSS}</style></head><body>'
           f'<header><b>DevOps AI Agent</b><a href="/pricing">Pricing</a><a href="/signup">Sign up</a><a href="/account">Account</a></header>'
           f'<main>{body}</main>'
           '<footer><a href="/terms">Terms</a><a href="/privacy">Privacy</a><a href="/dpa">DPA</a><a href="/refunds">Refunds</a></footer>'
           + (f'<script nonce="{nonce}">{script}</script>' if script else "") + '</body></html>')
    csp = (f"default-src 'none'; script-src 'nonce-{nonce}'; style-src 'nonce-{nonce}'; connect-src 'self'; "
           "img-src 'self' data:; base-uri 'none'; form-action 'self'; frame-ancestors 'none'")
    headers = {"Content-Security-Policy": csp, "X-Content-Type-Options": "nosniff", "Referrer-Policy": "no-referrer",
               "X-Frame-Options": "DENY"}
    if not cache:
        headers["Cache-Control"] = "no-store"
    return HTMLResponse(doc, headers=headers)


def _fmt(n) -> str:
    return "Unlimited" if n is None else f"{n:,}"


@web_router.get("/pricing", response_class=HTMLResponse)
def pricing():
    contact = os.getenv("PRODUCT_CONTACT_URL", "")
    opened = signup_open()
    cards = []
    for p in _ordered_plans():
        e = html.escape
        items = [f"<li>{_fmt(p.monthly_quota.get(ev))} {e(INLINE[ev])} / month</li>" for ev in LABELS if ev in p.monthly_quota]
        items.append(f"<li>{p.rate_per_minute:,} requests / minute</li>")
        items.append("<li>Usage beyond the included amount is billed</li>" if not p.hard_limit
                     else "<li>Requests stop at the monthly limit</li>")
        buy = subscriptions.purchasable(p.name)
        contact_btn = f'<a class="btn alt" href="{e(contact, quote=True)}">Contact us</a>' if contact else ""
        if p.name == "free":
            cta = ('<a class="btn" href="/signup">Start free</a>' if opened else
                   contact_btn or '<a class="btn" aria-disabled="true" href="#">Signup is closed</a>')
        elif buy:  # with signup closed, only people who already have an account (a key) can upgrade
            href = f"/signup?plan={e(p.name)}" if opened else f"/account?upgrade={e(p.name)}"
            cta = f'<a class="btn" href="{href}">Choose {e(p.name)}</a>'
        else:
            cta = contact_btn or '<a class="btn" aria-disabled="true" href="#">Not available yet</a>'
        price = e(p.price_label)  # same numbers Stripe is provisioned with (product/stripe_sync.py)
        cards.append(f'<div class="card"><h2>{e(p.name)}</h2><div class="price">{price}</div><ul>{"".join(items)}</ul>{cta}</div>')
    lead = ("Start on the free plan; upgrade any time from your account page. " if opened else
            "Sign-up is closed right now; if you already have an account, upgrade from your account page. ")
    body = (f'<h1>Pricing</h1><p class="lead">{lead}'
            'Paid plans are billed through Stripe.</p>'
            f'<div class="grid">{"".join(cards)}</div>'
            '<p class="fine">Quotas reset on the first of each month (UTC). Already have a key? <a href="/account">Manage your account</a>.</p>')
    return _page("Pricing", body)


@web_router.get("/signup", response_class=HTMLResponse)
def signup_page():
    recoverable = mailer.configured()
    if not signup_open():
        return _page("Sign up", '<h1>Sign up</h1><p class="lead">Signup is currently closed. '
                                '<a href="/pricing">See pricing</a> or contact us for access.'
                                + (' Lost your key? <a href="/recover">Recover it by email</a>.' if recoverable else "") + '</p>')
    lost = ('Lost it? <a href="/recover">Recover it with the email you signed up with</a>.' if recoverable
            else 'Lost it? Ask the operator for a new key, quoting the email you signed up with.')
    body = ('<h1>Create your account</h1><p class="lead">You get an API key right away on the free plan. No card needed.</p>'
            '<form id="f" class="box"><label for="name">Team or company name</label>'
            '<input id="name" name="name" maxlength="100" required autocomplete="organization">'
            '<label for="email">Email</label>'
            '<input id="email" name="email" type="email" maxlength="254" required autocomplete="email">'
            '<p class="fine tight">Only used to recover a lost key and for service notices. No marketing.</p>'
            '<label class="chk"><input id="terms" name="terms" type="checkbox" required> I accept the <a href="/terms">Terms of Service</a> '
            'and have read the <a href="/privacy">Privacy Policy</a>. If I send personal data, the <a href="/dpa">DPA</a> applies.</label>'
            '<div class="row"><button type="submit">Create account</button></div>'
            '<div id="msg" class="msg" role="status"></div></form>'
            '<div id="done" class="box" hidden><p>Your API key (shown once, copy it now):</p><p><code id="key"></code></p>'
            '<div class="row"><button id="copy" class="alt" type="button">Copy key</button>'
            '<a id="next" class="btn" href="/account">Go to your account</a></div>'
            f'<p class="fine">Send it as the <code>X-API-Key</code> header. {lost}</p></div>')
    script = """
const $=id=>document.getElementById(id);
const want=new URLSearchParams(location.search).get('plan')||'';
$('f').addEventListener('submit',async ev=>{
  ev.preventDefault();$('msg').className='msg';$('msg').textContent='Creating…';
  try{
    const r=await fetch('/signup',{method:'POST',headers:{'content-type':'application/json'},body:JSON.stringify({name:$('name').value,email:$('email').value,accept_terms:$('terms').checked})});
    const b=await r.json().catch(()=>({}));
    if(!r.ok)throw new Error(typeof b.detail==='string'?b.detail:'Signup failed ('+r.status+')');
    try{sessionStorage.setItem('adk_key',b.api_key)}catch(e){}
    $('key').textContent=b.api_key;$('f').hidden=true;$('done').hidden=false;
    if(/^[a-z0-9_-]+$/i.test(want))$('next').href='/account?upgrade='+encodeURIComponent(want);
  }catch(e){$('msg').className='msg err';$('msg').textContent=e.message}
});
$('copy').addEventListener('click',()=>{navigator.clipboard&&navigator.clipboard.writeText($('key').textContent)});
"""
    return _page("Sign up", body, script, cache=False)


@web_router.get("/recover", response_class=HTMLResponse)
def recover_page():
    if not mailer.configured():
        return _page("Recover your key", '<h1>Recover your API key</h1><p class="lead">Recovery by email is not set up on '
                                         'this server. Ask the operator for a new key, quoting the email you signed up with.</p>')
    body = ('<h1>Recover your API key</h1>'
            f'<form id="ask" class="box"><p class="lead">Enter the email you signed up with. We will send a one-time link, '
            f'valid for {store.RECOVERY_TTL // 60} minutes.</p>'
            '<label for="email">Email</label><input id="email" name="email" type="email" maxlength="254" required autocomplete="email">'
            '<div class="row"><button type="submit">Email me a link</button></div></form>'
            '<div id="redeem" class="box" hidden><p class="lead">Creating a new key <b>revokes all your existing keys</b>: '
            'anything still using an old key stops working until you give it the new one.</p>'
            '<div class="row"><button id="go" type="button">Create a new key</button></div></div>'
            '<div id="done" class="box" hidden><p>Your new API key (shown once, copy it now):</p><p><code id="key"></code></p>'
            '<div class="row"><button id="copy" class="alt" type="button">Copy key</button>'
            '<a class="btn" href="/account">Go to your account</a></div></div>'
            '<div id="msg" class="msg box" role="status"></div>')
    script = """
const $=id=>document.getElementById(id);
const say=(t,cls)=>{$('msg').className='msg box '+(cls||'');$('msg').textContent=t};
let tok='';
function pick(){  // also on hashchange: pasting the emailed link into a tab that already shows /recover only changes the fragment
  const t=new URLSearchParams(location.hash.slice(1)).get('t')||'';
  if(!t)return;
  tok=t;history.replaceState(null,'',location.pathname);
  $('ask').hidden=true;$('done').hidden=true;$('redeem').hidden=false;say('');
}
pick();addEventListener('hashchange',pick);
async function post(path,body){
  const r=await fetch(path,{method:'POST',headers:{'content-type':'application/json'},body:JSON.stringify(body)});
  const b=await r.json().catch(()=>({}));
  if(!r.ok)throw new Error(typeof b.detail==='string'?b.detail:'Request failed ('+r.status+')');
  return b;
}
$('ask').addEventListener('submit',async ev=>{
  ev.preventDefault();say('Sending…');
  try{const b=await post('/recover',{email:$('email').value});say(b.status,'ok')}catch(e){say(e.message,'err')}
});
$('go').addEventListener('click',async()=>{
  say('Creating…');
  try{
    const b=await post('/recover/confirm',{token:tok});
    try{sessionStorage.setItem('adk_key',b.api_key)}catch(e){}
    $('key').textContent=b.api_key;$('redeem').hidden=true;$('done').hidden=false;say('');
  }catch(e){$('redeem').hidden=true;$('ask').hidden=false;say(e.message+'. Request a new link below.','err')}
});
$('copy').addEventListener('click',()=>{navigator.clipboard&&navigator.clipboard.writeText($('key').textContent)});
"""
    return _page("Recover your key", body, script, cache=False)


@web_router.get("/account", response_class=HTMLResponse)
def account_page():
    body = ('<h1>Account &amp; billing</h1><p class="lead">Paste your API key to see your plan and usage, upgrade, or manage billing.</p>'
            '<form id="kf" class="box"><label for="k">API key</label><input id="k" type="password" autocomplete="off" placeholder="adk_…">'
            '<div class="row"><button type="submit">Load account</button></div></form>'
            '<div id="msg" class="msg" role="status"></div>'
            '<section id="acct" hidden><h2 id="who"></h2><p>Plan: <b id="plan"></b> <span id="status"></span></p>'
            '<table><thead><tr><th>Usage this month</th><th>Used</th><th>Included</th></tr></thead><tbody id="usage"></tbody></table>'
            '<div class="row" id="actions"></div></section>')
    script = """
const $=id=>document.getElementById(id);
const LABEL={analyze:'Analyze runs',cost_check:'Cost checks',llm_tokens:'LLM tokens'};
let key='';try{key=sessionStorage.getItem('adk_key')||''}catch(e){}
const params=new URLSearchParams(location.search);
function say(t,cls){$('msg').className='msg '+(cls||'');$('msg').textContent=t}
async function api(path,opts){
  const r=await fetch(path,Object.assign({headers:{'x-api-key':key,'content-type':'application/json'}},opts||{}));
  const b=await r.json().catch(()=>({}));
  if(!r.ok)throw new Error(typeof b.detail==='string'?b.detail:'Request failed ('+r.status+')');
  return b;
}
async function go(path,body){
  say('Redirecting to Stripe…');
  try{const b=await api(path,{method:'POST',body:JSON.stringify(body)});location.href=b.url}
  catch(e){say(e.message,'err')}
}
function btn(text,alt,fn){const b=document.createElement('button');b.type='button';b.textContent=text;if(alt)b.className='alt';b.onclick=fn;$('actions').appendChild(b)}
function fmt(n){return n===null||n===undefined?'Unlimited':Number(n).toLocaleString()}
async function load(){
  if(!key){return}
  say('Loading…');
  try{
    const [bill,use]=await Promise.all([api('/billing'),api('/usage')]);
    $('who').textContent=bill.name;$('plan').textContent=bill.plan;
    $('status').textContent=bill.subscription_status?'('+bill.subscription_status+')':'';
    const tb=$('usage');tb.textContent='';
    for(const [ev,u] of Object.entries(use.usage)){
      const tr=document.createElement('tr');
      for(const v of [LABEL[ev]||ev,fmt(u.used),fmt(u.included)]){const td=document.createElement('td');td.textContent=v;tr.appendChild(td)}
      tb.appendChild(tr);
    }
    $('actions').textContent='';
    const back=location.origin+'/account';
    for(const p of bill.purchasable)btn('Upgrade to '+p,false,()=>go('/billing/checkout',{plan:p,success_url:back+'?checkout=success',cancel_url:back}));
    if(bill.can_manage)btn('Manage billing',true,()=>go('/billing/portal',{return_url:back}));
    $('acct').hidden=false;say('');
    const up=params.get('upgrade');
    if(up&&bill.purchasable.includes(up)){history.replaceState(null,'',location.pathname);go('/billing/checkout',{plan:up,success_url:back+'?checkout=success',cancel_url:back})}
    else if(params.get('checkout')==='success'){say('Payment received. Your plan updates as soon as Stripe confirms it (usually seconds).','ok');setTimeout(()=>{params.delete('checkout');load()},4000)}
  }catch(e){$('acct').hidden=true;say(e.message,'err')}
}
$('kf').addEventListener('submit',ev=>{ev.preventDefault();key=$('k').value.trim();try{sessionStorage.setItem('adk_key',key)}catch(e){}load()});
load();
"""
    return _page("Account", body, script, cache=False)


# ---- legal documents ---------------------------------------------------------------------------------

def _legal_page(slug: str) -> HTMLResponse:
    body = legal.render(slug)
    if body is None:
        r = _page(legal.TITLES[slug], f'<h1>{html.escape(legal.TITLES[slug])}</h1><p class="lead">This document has not '
                                      'been published yet.</p>')
        r.status_code = 503
        return r
    return _page(legal.TITLES[slug], body)


for _slug in legal.TITLES:
    web_router.add_api_route(f"/{_slug}", (lambda s=_slug: lambda: _legal_page(s))(), methods=["GET"],
                             response_class=HTMLResponse, name=f"legal_{_slug}")
