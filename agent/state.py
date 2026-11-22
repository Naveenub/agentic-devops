from typing import List, TypedDict


class Observation(TypedDict, total=False):
    source: str      # e.g. "kubectl logs", "github actions"
    content: str
    mock: bool       # True: placeholder data, never used as evidence
    error: bool      # True: tool failed, content is the error message


class AgentState(TypedDict, total=False):
    # input
    question: str
    kubernetes: dict          # optional: {"namespace": str, "pod": str}
    github: dict              # optional: {"repo": str, "run_id": str}
    # observe
    observations: List[Observation]
    # diagnose
    category: str             # see agent.signatures; "unknown" if no signature matched
    diagnosis: str
    evidence: List[str]
    rag_sources: List[str]
    # suggest
    suggestions: List[str]
    confidence: float
    answer: str


def real_observations(state: AgentState) -> List[Observation]:
    """Observations that may be used as evidence (no mocks, no tool errors)."""
    return [
        o for o in state.get("observations", [])
        if not o.get("mock") and not o.get("error") and o.get("content")
    ]
