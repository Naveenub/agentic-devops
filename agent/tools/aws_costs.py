from agent.memory.chroma import query_knowledge

def analyze_cost_anomaly(service: str, region: str):
    """
    Detect AWS cost anomalies using historical cost incidents.
    """

    query = f"AWS {service} cost spike in {region}"

    results = query_knowledge(
        query,
        n_results=5,
        where={"type": "cost_incident"}
    )

    documents = results.get("documents", [[]])[0]

    if not documents:
        return {
            "anomaly": False,
            "confidence": 0.1,
            "message": "No historical cost anomalies found."
        }

    # MVP heuristic (std-dev / z-score logic placeholder)
    confidence = min(0.9, 0.3 + (len(documents) * 0.1))

    return {
        "anomaly": True,
        "confidence": confidence,
        "message": f"Detected similar AWS {service} cost anomalies in historical data."
    }
