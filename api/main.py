import asyncio
import os
from contextlib import asynccontextmanager

from fastapi import APIRouter, FastAPI, HTTPException

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


@router.post("/slack/command")
async def slack_command(payload: dict):
    from agent.tools.slack import send_message  # lazy: slack_sdk only needed here

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
