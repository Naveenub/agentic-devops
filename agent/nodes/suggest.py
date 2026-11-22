from agent.confidence import calculate_confidence
from agent.signatures import SUGGESTIONS, UNKNOWN
from agent.state import AgentState


def suggest(state: AgentState) -> dict:
    category = state.get("category", UNKNOWN)
    suggestions = list(SUGGESTIONS.get(category, SUGGESTIONS[UNKNOWN]))
    confidence = calculate_confidence(state)
    answer = f'{state.get("diagnosis", "")}\nSuggested fix: {suggestions[0]}'
    return {"suggestions": suggestions, "confidence": confidence, "answer": answer}
