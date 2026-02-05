from fastapi import FastAPI
from agent.graph import build_graph
from agent.state import AgentState

from fastapi import APIRouter
from agent.graph import agent
from agent.tools.slack import send_message

app = FastAPI(title="DevOps AI Agent")

router = APIRouter()

graph = build_graph()

@app.post("/analyze")
def analyze(payload: dict):
    state = AgentState(query=payload["query"])
    result = graph.invoke(state)
    return {
        "answer": result.answer,
        "confidence": result.confidence,
        "sources": result.sources
    }
    
@router.post("/slack/command")
async def slack_command(payload: dict):
    channel = payload["channel_id"]
    question = payload["text"]

    result = agent.invoke({"question": question})

    response = f"""
*Diagnosis:* {result['diagnosis']}
*Confidence:* {result['confidence']}
*Evidence:* {', '.join(result.get('evidence', []))}
"""

    send_message(channel, response)
    return {"ok": True}
    
