import asyncio
import hashlib
import hmac
import json
import os
import time
from contextlib import asynccontextmanager
from urllib.parse import parse_qs

from fastapi import APIRouter, FastAPI, HTTPException, Request

from agent.cost_watch import check_cost, parse_watch, watch_forever
from agent.graph import build_graph


@asynccontextmanager
async def lifespan(_app):
    """Start the cost scheduler only when COST_WATCH lists something to watch."""
    watch = parse_watch(os.getenv("COST_WATCH"))
    task = asyncio.create_task(watch_forever(
        watch, float(os.getenv("COST_CHECK_INTERVAL_SECONDS", "21600")),
        float(os.getenv("COST_ALERT_COOLDOWN_SECONDS", "86400")))) if watch else None
    yield
    if task:
        task.cancel()


app = FastAPI(title="DevOps AI Agent", lifespan=lifespan)
router = APIRouter()
graph = build_graph()


def _run(question: str, extra: dict | None = None) -> dict:
    state = {"question": question}
    state.update({k: v for k, v in (extra or {}).items() if k in ("kubernetes", "github", "cost")})
    return graph.invoke(state)


@app.post("/analyze")
def analyze(payload: dict):
    question = payload.get("question") or payload.get("query")
    if not question:
        raise HTTPException(status_code=422, detail="'question' is required")
    result = _run(question, payload)
    return {
        "answer": result["answer"],
        "diagnosis": result["diagnosis"],
        "suggestions": result["suggestions"],
        "confidence": result["confidence"],
        "evidence": result["evidence"],
        "sources": result["rag_sources"],
    }


@app.post("/cost/check")
def cost_check(payload: dict):
    """Run the agent on one service/region's daily spend; alert on a z-score spike.
    Alert targets come from SLACK_ALERT_CHANNEL / PAGERDUTY_ROUTING_KEY only, never the request."""
    service, region = payload.get("service"), payload.get("region")
    if not service or not region:
        raise HTTPException(status_code=422, detail="'service' and 'region' are required")
    try:
        threshold = float(payload.get("threshold", 2.0))
    except (TypeError, ValueError):
        raise HTTPException(status_code=422, detail="'threshold' must be a number")
    return check_cost(service, region, threshold)


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


@router.post("/slack/command")
async def slack_command(request: Request):
    from agent.tools.slack import send_message  # lazy: slack_sdk only needed here

    raw = await request.body()
    _verify_slack(raw, request.headers.get("x-slack-request-timestamp"), request.headers.get("x-slack-signature"))
    # Slack posts slash commands form-encoded; JSON is accepted too.
    payload = (json.loads(raw) if "json" in request.headers.get("content-type", "")
               else {k: v[0] for k, v in parse_qs(raw.decode()).items()})

    tokens = payload["text"].split()
    if tokens and tokens[0].lower() == "cost":  # `cost <service> <region>`, e.g. `cost ec2 us-east-1`
        if len(tokens) < 3:
            send_message(payload["channel_id"], "Usage: `cost <service> <region>` e.g. `cost ec2 us-east-1`")
        else:
            r = check_cost(" ".join(tokens[1:-1]), tokens[-1], notify=False)
            a = r["cost_anomaly"]
            send_message(payload["channel_id"], f"*Cost:* {a['message']}"
                         + (f"\n*Suggested:* {r['suggestions'][0]}" if a["anomaly"] else ""))
        return {"ok": True}

    result = _run(payload["text"])
    send_message(
        payload["channel_id"],
        f"*Diagnosis:* {result['diagnosis']}\n"
        f"*Confidence:* {result['confidence']}\n"
        f"*Evidence:* {', '.join(result['evidence'])}",
    )
    return {"ok": True}


app.include_router(router)
