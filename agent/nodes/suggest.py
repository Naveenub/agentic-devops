def suggest(state):
    state.suggestion = (
        "Increase container memory limits and redeploy."
    )
    state.confidence = min(1.0, len(state.sources) / 3)
    state.answer = f"{state.diagnosis}\nSuggested fix: {state.suggestion}"
    return state
