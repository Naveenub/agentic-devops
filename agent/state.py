from typing import List, TypedDict

LLM_CATEGORY = "other_llm"  # category for a diagnosis proposed by the LLM with no regex signature


class Observation(TypedDict, total=False):
    source: str      # e.g. "kubectl logs", "github actions"
    content: str
    mock: bool       # True: placeholder data, never used as evidence (e.g. test fixtures)
    error: bool      # True: tool failed, content is the error message


class AgentState(TypedDict, total=False):
    # input
    question: str
    kubernetes: dict          # optional: {"namespace": str, "pod": str}
    github: dict              # optional: {"repo": "owner/name", "run_id": "123456789"}; needs GITHUB_TOKEN
    cost: dict                # optional: {"service": "EC2", "region": "us-east-1", "threshold": 2.0}; read-only Cost Explorer
    fix: dict                 # optional: {"repo","path","content","title","base","summary"}; queued for human approval, never applied
    # observe
    observations: List[Observation]
    cost_anomaly: dict        # analyze_cost_anomaly() result; only set when `cost` was supplied
    # diagnose
    category: str             # see agent.signatures; LLM_CATEGORY or "unknown" otherwise
    diagnosis: str
    diagnosis_source: str     # "signature" (regex, terraform parser or cost z-score) | "signature+llm" | "llm" | "none"
    evidence: List[str]
    terraform: dict           # parse_error() result; only set when it agrees with the category
    rag_sources: List[str]
    rag_scores: List[float]   # retrieval scores, parallel to rag_sources
    llm_suggestions: List[str]
    # suggest
    suggestions: List[str]
    confidence: float
    answer: str
    # execute
    fix_proposal: dict        # {"id","status"} when queued; {"status","error"} when it could not be


def real_observations(state: AgentState) -> List[Observation]:
    """Observations that may be used as evidence (no mocks, no tool errors)."""
    return [
        o for o in state.get("observations", [])
        if not o.get("mock") and not o.get("error") and o.get("content")
    ]
