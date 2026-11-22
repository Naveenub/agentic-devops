import asyncio
import hashlib
import hmac
import json
import logging
import os
import time
from contextlib import asynccontextmanager
from urllib.parse import parse_qs

from fastapi import APIRouter, BackgroundTasks, Depends, FastAPI, Header, HTTPException, Request

from agent import approvals, llm
from agent.cost_watch import check_cost, parse_watch, watch_forever
from agent.graph import build_graph


@asynccontextmanager
async def lifespan(_app):
    """Start the cost scheduler only when COST_WATCH lists something to watch, and the billing scheduler only when
    the product layer is on and PRODUCT_BILLING_INTERVAL is set."""
    watch = parse_watch(os.getenv("COST_WATCH"))
    task = asyncio.create_task(watch_forever(
        watch, float(os.getenv("COST_CHECK_INTERVAL_SECONDS", "21600")),
        float(os.getenv("COST_ALERT_COOLDOWN_SECONDS", "86400")))) if watch else None
    billing_task = None
    if product_enabled():  # commercial layer only; PRODUCT_BILLING_INTERVAL unset = no scheduler (use cron)
        try:
            from product.scheduler import billing_forever, interval
        except ImportError:
            pass
        else:
            if interval():
                billing_task = asyncio.create_task(billing_forever(interval()))
    yield
    for t in (task, billing_task):
        if t:
            t.cancel()


# Schema/docs routes are off: they would publish the API surface unauthenticated.
app = FastAPI(title="DevOps AI Agent", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
router = APIRouter()
graph = build_graph()


def require_api_key(x_api_key: str | None = Header(None)) -> None:
    """Shared-secret guard for /analyze and /cost/check. Fails closed when AGENT_API_KEY is unset."""
    key = os.getenv("AGENT_API_KEY")
    if not key:
        raise HTTPException(status_code=503, detail="AGENT_API_KEY is not configured")
    if not hmac.compare_digest((x_api_key or "").encode(), key.encode()):
        raise HTTPException(status_code=401, detail="invalid API key")


def product_enabled() -> bool:
    """Commercial layer (product/) is on only when PRODUCT_DB is set; the Apache-2.0 core runs without it."""
    return bool(os.getenv("PRODUCT_DB"))


def _auth(event: str):
    """Per-route auth. Core mode: the shared AGENT_API_KEY, no tenant. Product mode: a per-tenant
    API key with rate limit and (on hard-limit plans) quota; the dependency returns the tenant."""
    def dep(x_api_key: str | None = Header(None)):
        if not product_enabled():
            require_api_key(x_api_key)
            return None
        from product.auth import current_tenant
        from product.metering import check_quota

        tenant = current_tenant(x_api_key)
        check_quota(tenant, event)
        return tenant
    return dep


def _reserve(tenant: dict | None, event: str) -> int | None:
    """Atomic quota/spend-cap check + usage write (429 if refused). The row id goes to _release on failure."""
    if tenant is None:
        return None
    from product.metering import reserve

    return reserve(tenant, event)


def _release(row_id: int | None) -> None:
    if row_id is not None:
        from product.metering import release

        release(row_id)


def _run(question: str, extra: dict | None = None) -> dict:
    state = {"question": question}
    state.update({k: v for k, v in (extra or {}).items() if k in ("kubernetes", "github", "cost")})
    logs = (extra or {}).get("logs")
    if isinstance(logs, str) and logs.strip():  # caller-supplied log text: no server credentials needed
        state["observations"] = [{"source": "supplied logs", "content": logs[-20000:]}]
    return graph.invoke(state)


def _gate_shared_tools(tenant: dict | None, payload: dict) -> None:
    """Tenants may not point the server's own kubectl/GitHub/AWS credentials at their requests
    unless their plan grants shared_tools."""
    if tenant is None:
        return
    from product.auth import allowed_inputs

    denied = ({"kubernetes", "github", "cost"} & set(payload)) - allowed_inputs(tenant)
    if denied:
        raise HTTPException(status_code=403, detail=f"inputs not available on plan '{tenant['plan']}': {sorted(denied)}")


def _gate_cost_check(tenant: dict | None) -> None:
    if tenant is None:
        return
    from product.auth import can_cost_check

    if not can_cost_check(tenant):
        raise HTTPException(status_code=403, detail=f"cost checks not available on plan '{tenant['plan']}'")


@app.get("/healthz")
def healthz():
    return {"ok": True}


@app.post("/analyze")
def analyze(payload: dict, tenant: dict | None = Depends(_auth("analyze"))):
    question = payload.get("question") or payload.get("query")
    if not question:
        raise HTTPException(status_code=422, detail="'question' is required")
    _gate_shared_tools(tenant, payload)
    tokens: list = []
    rid = _reserve(tenant, "analyze")
    reset = llm.usage_sink.set(tokens)
    try:
        result = _run(question, payload)
    except BaseException:
        _release(rid)
        raise
    finally:
        llm.usage_sink.reset(reset)
    if tenant:
        from product.metering import record

        record(tenant, "llm_tokens", sum(tokens))
    return {
        "answer": result["answer"],
        "diagnosis": result["diagnosis"],
        "suggestions": result["suggestions"],
        "confidence": result["confidence"],
        "evidence": result["evidence"],
        "sources": result["rag_sources"],
    }


@app.post("/cost/check")
def cost_check(payload: dict, tenant: dict | None = Depends(_auth("cost_check"))):
    """Run the agent on one service/region's daily spend; alert on a z-score spike.
    Alert targets come from SLACK_ALERT_CHANNEL / PAGERDUTY_ROUTING_KEY only, never the request."""
    service, region = payload.get("service"), payload.get("region")
    if not service or not region:
        raise HTTPException(status_code=422, detail="'service' and 'region' are required")
    try:
        threshold = float(payload.get("threshold", 2.0))
    except (TypeError, ValueError):
        raise HTTPException(status_code=422, detail="'threshold' must be a number")
    _gate_cost_check(tenant)  # reads the operator's AWS account and posts to the operator's Slack/PagerDuty
    rid = _reserve(tenant, "cost_check")
    try:
        return check_cost(service, region, threshold)
    except BaseException:
        _release(rid)
        raise


def _verify_slack(raw: bytes, ts: str | None, sig: str | None) -> None:
    """Slack request signing: HMAC-SHA256 of `v0:{ts}:{body}`, 5-minute replay window. Fails closed."""
    secret = os.getenv("SLACK_SIGNING_SECRET")
    if not secret:
        raise HTTPException(status_code=503, detail="SLACK_SIGNING_SECRET is not configured")
    try:
        fresh = abs(time.time() - int(ts)) <= 300
    except (TypeError, ValueError):
        fresh = False
    expected = "v0=" + hmac.new(secret.encode(), b"v0:" + (ts or "").encode() + b":" + raw, hashlib.sha256).hexdigest()
    if not (fresh and hmac.compare_digest(expected.encode(), (sig or "").encode())):
        raise HTTPException(status_code=401, detail="invalid Slack signature")


def _slack_event(payload: dict) -> str:
    tokens = (payload.get("text") or "").split()
    return "cost_check" if tokens and tokens[0].lower() == "cost" else "analyze"


def _slack_tenant(payload: dict) -> tuple[dict | None, str | None]:
    """Product mode: the workspace must be linked to a tenant (PUT /admin/tenants/{id}/slack), and that
    tenant's rate limit, quota and shared-tool grant apply as on the HTTP API. Returns (tenant, refusal)."""
    if not product_enabled():
        return None, None
    from product import metering, store

    tenant = store.tenant_for_slack(payload.get("team_id"))
    if not tenant:
        return None, "This Slack workspace isn't linked to an account."
    event = _slack_event(payload)
    try:
        metering.check_rate(tenant)
        metering.check_quota(tenant, event)
        if event == "cost_check":  # reads the operator's AWS account
            _gate_cost_check(tenant)
    except HTTPException as e:
        return None, str(e.detail)
    return tenant, None


def _handle_slack(payload: dict, tenant: dict | None = None) -> None:
    """Runs after the 200 ack (Slack gives 3s) and in a worker thread, so the agent/AWS calls
    neither time the command out nor block the event loop. A tenant means usage is metered."""

    from agent.tools.slack import send_message  # lazy: slack_sdk only needed here

    channel = payload["channel_id"]
    rid = None
    try:
        tokens = payload["text"].split()
        if tokens and tokens[0].lower() == "cost":  # `cost <service> <region>`, e.g. `cost ec2 us-east-1`
            if len(tokens) < 3:
                send_message(channel, "Usage: `cost <service> <region>` e.g. `cost ec2 us-east-1`")
            else:
                rid = _reserve(tenant, "cost_check")
                r = check_cost(" ".join(tokens[1:-1]), tokens[-1], notify=False)
                a = r["cost_anomaly"]
                send_message(channel, f"*Cost:* {a['message']}"
                             + (f"\n*Suggested:* {r['suggestions'][0]}" if a["anomaly"] else ""))
            return
        used: list = []
        rid = _reserve(tenant, "analyze")
        reset = llm.usage_sink.set(used)
        try:
            result = _run(payload["text"])
        finally:
            llm.usage_sink.reset(reset)
        send_message(
            channel,
            f"*Diagnosis:* {result['diagnosis']}\n"
            f"*Confidence:* {result['confidence']}\n"
            f"*Evidence:* {', '.join(result['evidence'])}",
        )
        if tenant:
            from product.metering import record

            record(tenant, "llm_tokens", sum(used))
    except HTTPException as e:  # refused by quota / spend cap at reservation time
        send_message(channel, str(e.detail))
    except Exception:  # the ack already went out, so tell the channel instead of failing silently
        _release(rid)
        logging.getLogger(__name__).exception("slack command failed")
        send_message(channel, "Command failed; see server logs.")


# ---- human-in-the-loop fixes -> pull requests ----------------------------------------------------
# A caller proposes one file change; nothing happens until someone holding AGENT_APPROVER_KEY approves it, and the
# approval opens a PR (never a merge). Single-org feature: 404 in product (multi-tenant) mode, 503 until configured.

def require_approver(x_approver_key: str | None = Header(None)) -> None:
    key = os.getenv("AGENT_APPROVER_KEY")
    if not key or key == os.getenv("AGENT_API_KEY"):  # a requester must not be able to approve its own proposal
        raise HTTPException(status_code=503, detail="AGENT_APPROVER_KEY must be set and differ from AGENT_API_KEY")
    if not hmac.compare_digest((x_approver_key or "").encode(), key.encode()):
        raise HTTPException(status_code=401, detail="invalid approver key")


def _fixes_available() -> None:
    if product_enabled():
        raise HTTPException(status_code=404, detail="Not Found")
    if not approvals.enabled():
        raise HTTPException(status_code=503, detail="AGENT_APPROVALS_DB is not configured")


def _fix(fid: str) -> dict:
    fix = approvals.get(fid)
    if not fix:
        raise HTTPException(status_code=404, detail="no such fix")
    return fix


def _label(value, default: str) -> str:
    return " ".join(str(value or default).split())[:60] or default


@router.post("/fixes", status_code=201, dependencies=[Depends(require_api_key), Depends(_fixes_available)])
def propose_fix(payload: dict):
    from agent.tools.github import GitHubToolError, _check_repo
    from agent.tools.github_pr import MAX_CONTENT, check_path

    allowed = {r.strip() for r in os.getenv("AGENT_PR_REPOS", "").split(",") if r.strip()}
    repo = payload.get("repo")
    if repo not in allowed:  # fail closed: empty AGENT_PR_REPOS allows nothing
        raise HTTPException(status_code=403, detail="repo is not in AGENT_PR_REPOS")
    base = payload.get("base") or "main"
    title, content = payload.get("title"), payload.get("content")
    try:
        _check_repo(repo), check_path(payload.get("path"))
    except GitHubToolError as e:
        raise HTTPException(status_code=422, detail=str(e))
    if not isinstance(title, str) or not title.strip() or len(title) > 200:
        raise HTTPException(status_code=422, detail="'title' is required (at most 200 characters)")
    if not isinstance(content, str) or len(content) > MAX_CONTENT:
        raise HTTPException(status_code=422, detail=f"'content' must be text of at most {MAX_CONTENT} characters")
    if not isinstance(base, str):
        raise HTTPException(status_code=422, detail="'base' must be a branch name")
    fix = approvals.create(repo, base, payload["path"], content, title.strip(), str(payload.get("summary") or "")[:2000],
                           _label(payload.get("requested_by"), "api"))
    return {"id": fix["id"], "status": fix["status"]}


@router.get("/fixes", dependencies=[Depends(require_api_key), Depends(_fixes_available)])
def list_fixes():
    return {"pending": approvals.pending()}


@router.get("/fixes/{fid}", dependencies=[Depends(require_api_key), Depends(_fixes_available)])
def show_fix(fid: str):
    return _fix(fid)  # includes the full proposed content, which is what the approver reviews


@router.post("/fixes/{fid}/reject", dependencies=[Depends(require_approver), Depends(_fixes_available)])
def reject_fix(fid: str, x_approver: str | None = Header(None)):
    _fix(fid)
    if not approvals.claim(fid, "rejected", _label(x_approver, "approver")):
        raise HTTPException(status_code=409, detail="fix is not pending")
    return {"id": fid, "status": "rejected"}


@router.post("/fixes/{fid}/approve", dependencies=[Depends(require_approver), Depends(_fixes_available)])
def approve_fix(fid: str, x_approver: str | None = Header(None)):
    from agent.tools.github import GitHubToolError
    from agent.tools.github_pr import open_fix_pr

    fix = _fix(fid)
    allowed = {r.strip() for r in os.getenv("AGENT_PR_REPOS", "").split(",") if r.strip()}
    if fix["repo"] not in allowed:  # the allowlist may have shrunk since the proposal was made
        raise HTTPException(status_code=403, detail="repo is not in AGENT_PR_REPOS")
    who = _label(x_approver, "approver")
    if not approvals.claim(fid, "approving", who):  # atomic: two approvals cannot both open a PR
        raise HTTPException(status_code=409, detail="fix is not pending")
    try:
        pr = open_fix_pr(fix["repo"], fix["base"], f"agent-fix/{fid}", fix["path"], fix["content"], fix["title"],
                         f"{fix['summary']}\n\n---\nProposed by {fix['requested_by']}, approved by {who}. "
                         "Opened by the DevOps AI Agent after human approval; review before merging.")
    except GitHubToolError as e:
        approvals.finish(fid, "failed", error=str(e))
        raise HTTPException(status_code=502, detail=str(e))
    approvals.finish(fid, "opened", pr_url=pr["url"])
    return {"id": fid, "status": "opened", "pr_url": pr["url"]}


@router.post("/slack/command")
async def slack_command(request: Request, background: BackgroundTasks):
    raw = await request.body()
    _verify_slack(raw, request.headers.get("x-slack-request-timestamp"), request.headers.get("x-slack-signature"))
    # Slack posts slash commands form-encoded; JSON is accepted too.
    payload = (json.loads(raw) if "json" in request.headers.get("content-type", "")
               else {k: v[0] for k, v in parse_qs(raw.decode()).items()})
    allowed = {c.strip() for c in os.getenv("SLACK_ALLOWED_CHANNELS", "").split(",") if c.strip()}
    if allowed and payload.get("channel_id") not in allowed:
        return {"response_type": "ephemeral", "text": "This command isn't enabled in this channel."}
    tenant, refusal = _slack_tenant(payload)
    if refusal:
        return {"response_type": "ephemeral", "text": refusal}
    background.add_task(_handle_slack, payload, tenant)
    return {"ok": True}


app.include_router(router)

try:  # commercial layer; absent from the open-core image. Its routes answer 404 unless PRODUCT_DB is set.
    from product.admin import admin, tenant_router, webhook_router
    from product.web import web_router
except ImportError:
    pass
else:
    app.include_router(admin)
    app.include_router(tenant_router)
    app.include_router(webhook_router)
    app.include_router(web_router)  # /pricing, /signup, /account, /billing
