"""Operator endpoints (X-Admin-Key) and the tenant's own usage view."""
import os

from fastapi import APIRouter, Depends, HTTPException

from product import billing, metering, store
from product.auth import current_tenant, require_admin

def require_enabled() -> None:
    if not os.getenv("PRODUCT_DB"):
        raise HTTPException(status_code=404, detail="Not Found")


admin = APIRouter(prefix="/admin", dependencies=[Depends(require_enabled), Depends(require_admin)])
tenant_router = APIRouter(dependencies=[Depends(require_enabled)])


def _bad(e: Exception):
    return HTTPException(status_code=422, detail=str(e).strip("'\""))


@admin.post("/tenants")
def create_tenant(payload: dict):
    name = payload.get("name")
    if not name:
        raise HTTPException(status_code=422, detail="'name' is required")
    try:
        t = store.create_tenant(name, payload.get("plan", "free"))
    except KeyError as e:
        raise _bad(e)
    return {**t, "api_key": store.issue_key(t["id"])}  # shown once


@admin.patch("/tenants/{tid}")
def update_tenant(tid: str, payload: dict):
    try:
        t = store.update_tenant(tid, plan=payload.get("plan"), active=payload.get("active"),
                                stripe_customer_id=payload.get("stripe_customer_id"))
    except KeyError as e:
        raise _bad(e)
    if not t:
        raise HTTPException(status_code=404, detail="no such tenant")
    return t


@admin.post("/tenants/{tid}/keys")
def new_key(tid: str):
    if not store.get_tenant(tid):
        raise HTTPException(status_code=404, detail="no such tenant")
    return {"api_key": store.issue_key(tid)}


@admin.delete("/tenants/{tid}/keys/{prefix}")
def revoke(tid: str, prefix: str):
    return {"revoked": store.revoke_key(prefix, tid)}


@admin.get("/usage/{tid}")
def tenant_usage(tid: str):
    t = store.get_tenant(tid)
    if not t:
        raise HTTPException(status_code=404, detail="no such tenant")
    return metering.summary(t)


@admin.post("/billing/report")
def report():
    return billing.report_usage()


@tenant_router.get("/usage")
def my_usage(tenant: dict = Depends(current_tenant)):
    return metering.summary(tenant)
