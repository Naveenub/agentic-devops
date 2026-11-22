from fastapi import APIRouter, FastAPI, HTTPException

from agent.graph import build_graph

app = FastAPI(title="DevOps AI Agent")
router = APIRouter()
graph = build_graph()


def _run(question: str, extra: dict | None = None) -> dict:
    state = {"question": question}
    state.update({k: v for k, v in (extra or {}).items() if k in ("kubernetes", "github")})
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
