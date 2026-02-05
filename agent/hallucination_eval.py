def hallucination_score(answer, sources):
    unsupported = [s for s in answer.split() if s not in sources]
    return len(unsupported) / len(answer.split())
