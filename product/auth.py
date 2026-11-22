"""FastAPI dependencies: tenant auth from X-API-Key, and an operator guard for /admin."""
import hmac
import os

from fastapi import Depends, Header, HTTPException

from product import metering, store
from product.plans import get_plan


def current_tenant(x_api_key: str | None = Header(None)) -> dict:
    tenant = store.tenant_for_key(x_api_key) if x_api_key else None
    if not tenant:
        raise HTTPException(status_code=401, detail="invalid API key")
    metering.check_rate(tenant)
    return tenant


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
