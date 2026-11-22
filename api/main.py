import os

from fastapi import APIRouter, FastAPI, HTTPException

from agent.graph import build_graph

app = FastAPI(title="DevOps AI Agent")
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
    """Run the agent on one service/region's daily spend; alert Slack on a z-score spike.
    The alert channel comes from SLACK_ALERT_CHANNEL only (never from the request)."""
    service, region = payload.get("service"), payload.get("region")
    if not service or not region:
        raise HTTPException(status_code=422, detail="'service' and 'region' are required")
    try:
        cost = {"service": service, "region": region, "threshold": float(payload.get("threshold", 2.0))}
    except (TypeError, ValueError):
        raise HTTPException(status_code=422, detail="'threshold' must be a number")

    result = _run(f"Cost check: {service} in {region}", {"cost": cost})
    anomaly = result["cost_anomaly"]
    response = {"cost_anomaly": anomaly, "diagnosis": result["diagnosis"],
                "suggestions": result["suggestions"], "alerted": False}

    channel = os.getenv("SLACK_ALERT_CHANNEL")
    if anomaly["anomaly"] is True and channel:
        from agent.tools.slack import send_message  # lazy: slack_sdk only needed here

        try:
            send_message(
                channel,
                f":rotating_light: *Cost anomaly* - {anomaly['message']}\n"
                f"*Latest:* {anomaly['latest']} vs mean {anomaly['mean']}\n"
                f"*Suggested:* {result['suggestions'][0]}",
            )
            response["alerted"] = True
        except Exception as e:  # detection succeeded; don't lose it because Slack failed
            response["alert_error"] = str(e)
    return response


@router.post("/slack/command")
async def slack_command(payload: dict):
    from agent.tools.slack import send_message  # lazy: slack_sdk only needed here

    result = _run(payload["text"])
    send_message(
        payload["channel_id"],
        f"*Diagnosis:* {result['diagnosis']}\n"
        f"*Confidence:* {result['confidence']}\n"
        f"*Evidence:* {', '.join(result['evidence'])}",
    )
    return {"ok": True}


app.include_router(router)
