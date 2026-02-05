from fastapi import FastAPI
from agent.graph import build_graph
from agent.state import AgentState

app = FastAPI(title="DevOps AI Agent")

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
