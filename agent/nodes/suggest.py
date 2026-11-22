from agent.confidence import calculate_confidence
from agent.signatures import SUGGESTIONS, UNKNOWN
from agent.state import AgentState


def _iam_fix(tf: dict) -> str | None:
    """Targeted least-privilege suggestion from the parsed denied action/resource."""
    if not tf.get("action"):
        return None
    where = f' on {tf["resource"]}' if tf.get("resource") else ""
    return f"Grant only {tf['action']}{where} to the CI role's least-privilege policy, then re-run terraform plan."


def suggest(state: AgentState) -> dict:
    category = state.get("category", UNKNOWN)
    templates = list(SUGGESTIONS.get(category, SUGGESTIONS[UNKNOWN]))
    llm_s = state.get("llm_suggestions", [])
    specific = _iam_fix(state.get("terraform") or {}) if category == "iam_denied" else None
    suggestions = ([specific] if specific else []) + llm_s + [t for t in templates if t not in llm_s]
    confidence = calculate_confidence(state)
    answer = f'{state.get("diagnosis", "")}\nSuggested fix: {suggestions[0]}'
    return {"suggestions": suggestions, "confidence": confidence, "answer": answer}
