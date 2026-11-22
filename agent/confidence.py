import os

from agent.signatures import CORROBORATING, UNKNOWN
from agent.state import LLM_CATEGORY, AgentState, real_observations


def calculate_confidence(state: AgentState) -> float:
    """0.0 if nothing is supported; 0.5 for a log signature; +0.3 if a retrieved runbook/incident
    corroborates it. An LLM-only diagnosis (verbatim-verified evidence) is capped at 0.4.
    Mock/error observations never count."""
    category = state.get("category", UNKNOWN)
    if category == UNKNOWN or not real_observations(state) or not state.get("evidence"):
        return 0.0
    if category == LLM_CATEGORY:
        return 0.4

    score = 0.5
    corroborating = CORROBORATING.get(category, set())
    if any(os.path.basename(s) in corroborating for s in state.get("rag_sources", [])):
        score += 0.3
    return round(min(score, 1.0), 2)
