def calculate_confidence(state):
    score = 0.0

    if state.get("observations"):
        score += 0.4

    if state.get("rag_sources"):
        score += 0.3

    if "uncertain" in state.get("diagnosis", "").lower():
        score -= 0.2

    if "no data" in state.get("diagnosis", "").lower():
        score -= 0.3

    return round(min(max(score, 0.0), 1.0), 2)
