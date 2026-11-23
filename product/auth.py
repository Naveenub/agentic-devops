"""FastAPI dependencies: tenant auth from X-API-Key, and an operator guard for /admin."""
import hmac
import os

from fastapi import Depends, Header, HTTPException, Request

from product import mailer, metering, sso, store
from product.plans import get_plan


def current_tenant(x_api_key: str | None = Header(None)) -> dict:
    tenant = store.tenant_for_key(x_api_key) if x_api_key else None
    if not tenant:
        raise HTTPException(status_code=401, detail="invalid API key")
    metering.check_rate(tenant)
    return tenant


def current_principal(request: Request, x_api_key: str | None = Header(None)) -> dict:
    """Who is calling the account endpoints (members, SSO settings, usage, billing state): a tenant API key, which
    acts as the tenant's owner, or a member's session cookie (from an accepted invitation or an SSO login).
    {"tenant", "member" (None for a key), "role", "via": "key"|"session"}. A key that is present but wrong is a 401,
    never a fall-through to the cookie. API-only endpoints (/analyze ...) keep using current_tenant."""
    if x_api_key:
        return {"tenant": current_tenant(x_api_key), "member": None, "role": "owner", "via": "key"}
    session = sso.unsign(request.cookies.get(sso.SESSION_COOKIE), "session")
    member = store.live_member(session["t"], session["m"]) if session else None
    tenant = store.get_tenant(member["tenant_id"]) if member else None
    if not (member and tenant):
        raise HTTPException(status_code=401, detail="send an API key (X-API-Key) or sign in")
    if request.method not in ("GET", "HEAD", "OPTIONS"):
        # SameSite=Lax already keeps other sites from sending the cookie on a POST; refuse a foreign Origin as well.
        origin = request.headers.get("origin")
        if origin and origin.rstrip("/") != mailer.public_url():
            raise HTTPException(status_code=403, detail="cross-origin request refused")
    metering.check_rate(tenant)
    return {"tenant": tenant, "member": member, "role": member["role"], "via": "session"}


def require_manager(p: dict = Depends(current_principal)) -> dict:
    """Owners and admins (and API keys) may manage members and SSO; plain members may only look."""
    if p["role"] not in ("owner", "admin"):
        raise HTTPException(status_code=403, detail="owners and admins only")
    return p


def require_admin(x_admin_key: str | None = Header(None)) -> None:
    """Fails closed when PRODUCT_ADMIN_KEY is unset."""
    key = os.getenv("PRODUCT_ADMIN_KEY")
    if not key:
        raise HTTPException(status_code=503, detail="PRODUCT_ADMIN_KEY is not configured")
    if not hmac.compare_digest((x_admin_key or "").encode(), key.encode()):
        raise HTTPException(status_code=401, detail="invalid admin key")


def metered(event: str):
    """Dependency factory: authenticate, enforce quota for `event`, return the tenant."""
    def dep(tenant: dict = Depends(current_tenant)) -> dict:
        metering.check_quota(tenant, event)
        return tenant
    return dep


def allowed_inputs(tenant: dict) -> set:
    """Inputs that use the server's own credentials are off unless the plan grants shared_tools:
    otherwise one tenant could read another's cluster, CI runs or AWS spend."""
    return {"kubernetes", "github", "cost"} if get_plan(tenant["plan"]).shared_tools else set()


def can_cost_check(tenant: dict) -> bool:
    """/cost/check and Slack `cost ...` read the operator's AWS Cost Explorer (about $0.01 per call).
    Allowed by the narrow `cost_checks` flag, so it does not also unlock the server's kubectl/GitHub."""
    plan = get_plan(tenant["plan"])
    return plan.cost_checks or plan.shared_tools
