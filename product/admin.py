"""Operator endpoints (X-Admin-Key) and the tenant's own usage view."""
import os

from fastapi import APIRouter, Depends, HTTPException, Request

from product import billing, metering, store, stripe_sync, subscriptions
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
    email = None
    if payload.get("email") is not None:  # optional for operators; self-serve signups always have one
        try:
            email = store.clean_email(payload["email"])
        except ValueError as e:
            raise _bad(e)
    try:
        t = store.create_tenant(name, payload.get("plan", "free"), email)
    except KeyError as e:
        raise _bad(e)
    except store.EmailTaken:
        raise HTTPException(status_code=409, detail="a tenant with that email already exists")
    if os.getenv("STRIPE_SECRET_KEY"):  # best effort: checkout creates the customer later if this fails
        try:
            subscriptions.ensure_customer(t)
        except subscriptions.StripeError:
            pass
    return {**store.get_tenant(t["id"]), "api_key": store.issue_key(t["id"])}  # shown once


@admin.get("/tenants")
def find_tenant(email: str):
    """Look a tenant up by the email it signed up with, e.g. to issue a lost key when mail recovery is not set up
    (then POST /admin/tenants/{id}/keys)."""
    try:
        t = store.tenant_for_email(store.clean_email(email))
    except ValueError as e:
        raise _bad(e)
    if not t:
        raise HTTPException(status_code=404, detail="no such tenant")
    return t


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


@admin.put("/tenants/{tid}/slack")
def link_slack(tid: str, payload: dict):
    team_id = payload.get("team_id")
    if not team_id:
        raise HTTPException(status_code=422, detail="'team_id' is required")
    if not store.get_tenant(tid):
        raise HTTPException(status_code=404, detail="no such tenant")
    store.link_slack(tid, team_id)
    return {"tenant": tid, "team_id": team_id}


@admin.delete("/tenants/{tid}/slack/{team_id}")
def unlink_slack(tid: str, team_id: str):
    return {"unlinked": store.unlink_slack(tid, team_id)}


@admin.get("/usage/{tid}")
def tenant_usage(tid: str):
    t = store.get_tenant(tid)
    if not t:
        raise HTTPException(status_code=404, detail="no such tenant")
    return metering.summary(t)


@admin.post("/billing/report")
def report():
    return billing.report_usage()


@admin.get("/billing/drift")
def billing_drift():
    """Stripe meters/prices vs product/plans.py. Empty list = in sync; fix with `python -m product.stripe_sync --apply`."""
    drift, _ = _stripe(stripe_sync.sync)
    return {"in_sync": not drift, "drift": drift}


@tenant_router.get("/usage")
def my_usage(tenant: dict = Depends(current_tenant)):
    return metering.summary(tenant)


def _stripe(fn, *a, **kw):
    try:
        return fn(*a, **kw)
    except subscriptions.StripeError as e:
        raise HTTPException(status_code=e.status, detail=str(e))
    except KeyError as e:
        raise _bad(e)


@tenant_router.post("/billing/checkout")
def checkout(payload: dict, tenant: dict = Depends(current_tenant)):
    """Start a Stripe Checkout subscription for the calling tenant; the plan changes when the webhook arrives."""
    plan = payload.get("plan")
    if not plan:
        raise HTTPException(status_code=422, detail="'plan' is required")
    return {"url": _stripe(subscriptions.create_checkout, tenant, plan, payload.get("success_url"),
                           payload.get("cancel_url"))}


@tenant_router.post("/billing/portal")
def portal(payload: dict | None = None, tenant: dict = Depends(current_tenant)):
    """Stripe billing portal: update card, change plan, cancel."""
    return {"url": _stripe(subscriptions.create_portal, tenant, (payload or {}).get("return_url"))}


@admin.post("/tenants/{tid}/checkout")
def admin_checkout(tid: str, payload: dict):
    t = store.get_tenant(tid)
    if not t:
        raise HTTPException(status_code=404, detail="no such tenant")
    plan = payload.get("plan")
    if not plan:
        raise HTTPException(status_code=422, detail="'plan' is required")
    return {"url": _stripe(subscriptions.create_checkout, t, plan, payload.get("success_url"),
                           payload.get("cancel_url"))}


# Not behind X-API-Key or X-Admin-Key: Stripe calls it, and the signature is the authentication.
webhook_router = APIRouter(dependencies=[Depends(require_enabled)])


@webhook_router.post("/stripe/webhook")
async def stripe_webhook(request: Request):
    secret = os.getenv("STRIPE_WEBHOOK_SECRET")
    if not secret:
        raise HTTPException(status_code=503, detail="STRIPE_WEBHOOK_SECRET is not configured")
    body = await request.body()
    if not subscriptions.verify_signature(body, request.headers.get("stripe-signature"), secret):
        raise HTTPException(status_code=400, detail="invalid signature")
    try:
        event = subscriptions.parse_event(body)
        result = subscriptions.handle_event(event)
    except (ValueError, KeyError, TypeError) as e:  # malformed or unmappable: retrying will not help, but surface it
        raise HTTPException(status_code=422, detail=f"unprocessable event: {e}")
    except subscriptions.StripeError as e:
        raise HTTPException(status_code=500, detail=str(e))  # transient: Stripe retries
    return {"received": True, "result": result}
