# agent/eval.py

from agent.memory.chroma import query_knowledge

def evaluate_hallucination(test_case: dict) -> dict:
    """
    Evaluate whether the agent response is grounded in retrieved knowledge.
    """

    query = test_case["query"]
    expected_sources = test_case.get("expected_sources", [])

    results = query_knowledge(query, n_results=5)

    retrieved_docs = results.get("documents", [[]])[0]
    retrieved_sources = results.get("metadatas", [[]])[0]

    if not retrieved_docs:
        return {
            "grounded": False,
            "score": 0.0,
            "reason": "No documents retrieved"
        }

    matched_sources = [
        meta["source"]
        for meta in retrieved_sources
        if meta.get("source") in expected_sources
    ]

    score = len(matched_sources) / max(len(expected_sources), 1)

    return {
        "grounded": score > 0.5,
        "score": round(score, 2),
        "matched_sources": matched_sources
    }
