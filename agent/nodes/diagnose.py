from agent.memory.chroma import query_knowledge

def diagnose(state):
    """
    Use RAG to diagnose the issue based on historical incidents and runbooks.
    """

    results = query_knowledge(state.query, n_results=3)

    documents = results.get("documents", [[]])[0]
    metadatas = results.get("metadatas", [[]])[0]

    if not documents:
        state.diagnosis = "Insufficient historical data to determine root cause."
        state.sources = []
        return state

    # Simple heuristic diagnosis for MVP
    if "oom" in " ".join(documents).lower():
        state.diagnosis = "Pod was OOMKilled due to insufficient memory limits."
    elif "accessdenied" in " ".join(documents).lower():
        state.diagnosis = "Terraform failed due to missing IAM permissions."
    else:
        state.diagnosis = "Issue matches known incidents but requires manual review."

    state.sources = [
        meta.get("source", "unknown")
        for meta in metadatas
    ]

    return state
