from agent.confidence import calculate_confidence
from agent.signatures import SUGGESTIONS, UNKNOWN
from agent.state import AgentState


def suggest(state: AgentState) -> dict:
    category = state.get("category", UNKNOWN)
    templates = list(SUGGESTIONS.get(category, SUGGESTIONS[UNKNOWN]))
    llm_s = state.get("llm_suggestions", [])
    suggestions = llm_s + [t for t in templates if t not in llm_s]
    confidence = calculate_confidence(state)
    answer = f'{state.get("diagnosis", "")}\nSuggested fix: {suggestions[0]}'
    return {"suggestions": suggestions, "confidence": confidence, "answer": answer}
