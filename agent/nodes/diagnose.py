from agent.memory.retrieval import retrieve
import os

from agent.signatures import CORROBORATING, DIAGNOSIS, UNKNOWN, classify
from agent.state import AgentState, real_observations


def diagnose(state: AgentState) -> dict:
    """Classify from real observations only; retrieval corroborates, it never decides."""
    obs = real_observations(state)
    category, evidence = UNKNOWN, []
    for o in obs:
        category, line = classify(o["content"])
        if category != UNKNOWN:
            evidence.append(f'{o["source"]}: {line}')
            break

    query = " ".join([state.get("question", "")] + [o["content"][:1000] for o in obs])
    rag_sources = [h.source for h in retrieve(query, n=3)] if category != UNKNOWN else []
    # cite only retrieved sources that actually match the diagnosed category
    corroborating = CORROBORATING.get(category, set())
    evidence += [f"retrieved: {s}" for s in rag_sources if os.path.basename(s) in corroborating]

    return {
        "category": category,
        "diagnosis": DIAGNOSIS[category],
        "evidence": evidence,
        "rag_sources": rag_sources,
    }
