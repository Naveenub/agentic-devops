"""Confidence is graded from evidence strength, not fixed per code path."""
from agent.confidence import MAX_CONFIDENCE, calculate_confidence

OBS = [{"source": "k", "content": "Container terminated with exit code 137"}]


def _state(**kw):
    s = {"category": "oom_killed", "observations": OBS, "evidence": ["k: x"], "rag_sources": [], "rag_scores": []}
    s.update(kw)
    return s


def test_corroboration_by_rank_and_score_is_graded():
    none = calculate_confidence(_state())
    top = calculate_confidence(_state(rag_sources=["data/runbooks/eks-oomkill.md", "x"], rag_scores=[5.0, 1.0]))
    low = calculate_confidence(_state(rag_sources=["x", "y", "data/runbooks/eks-oomkill.md"], rag_scores=[5.0, 4.0, 1.0]))
    assert none < low < top <= MAX_CONFIDENCE


def test_more_independent_support_raises_confidence():
    two = OBS + [{"source": "j", "content": "OOMKilled\nJava heap space"}]
    assert calculate_confidence(_state(observations=two)) > calculate_confidence(_state())


def test_conflicting_signature_lowers_confidence():
    mixed = [{"source": "k", "content": "Container terminated with exit code 137\nAccessDenied calling s3"}]
    assert calculate_confidence(_state(observations=mixed)) < calculate_confidence(_state())


def test_cost_confidence_grows_with_z_score():
    def c(z):
        return calculate_confidence({"category": "cost_anomaly", "observations": OBS, "evidence": ["c"],
                                     "cost_anomaly": {"z_score": z, "threshold": 2.0, "n_points": 30}})
    assert 0 < c(2.5) < c(4.0) < c(float("inf")) <= MAX_CONFIDENCE


def test_llm_only_graded_but_capped():
    def c(n):
        return calculate_confidence({"category": "other_llm", "observations": OBS,
                                     "evidence": [f"log: q{i}" for i in range(n)]})
    assert 0 < c(1) < c(3) <= 0.4
