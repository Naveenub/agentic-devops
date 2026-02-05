from agent.memory.chroma import query_knowledge

def hallucination_score(answer: str, query: str) -> float:
    """
    Simple hallucination metric:
    % of answer tokens not supported by retrieved context
    """

    results = query_knowledge(query, n_results=3)
    documents = results["documents"][0]

    context = " ".join(documents).lower()
    answer_tokens = answer.lower().split()

    unsupported = [
        token for token in answer_tokens
        if token.isalpha() and token not in context
    ]

    if not answer_tokens:
        return 0.0

    return round(len(unsupported) / len(answer_tokens), 3)


if __name__ == "__main__":
    sample_query = "payments-api pod restarting"
    sample_answer = "The pod was OOMKilled due to memory limits"

    score = hallucination_score(sample_answer, sample_query)
    print("Hallucination score:", score)
