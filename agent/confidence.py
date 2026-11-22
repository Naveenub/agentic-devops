import math
import os

from agent.signatures import COST_ANOMALY, CORROBORATING, UNKNOWN, signature_matches
from agent.state import LLM_CATEGORY, AgentState, real_observations

# Deterministic, additive score built from evidence the agent actually has. Nothing here is a
# learned probability: it is a ranking heuristic, so it is graded (more/better evidence -> higher)
# and bounded, never a fixed value per path. Calibrate the weights against data/eval/real.
SIGNATURE_BASE = 0.45     # a recognised failure signature in a real observation
LINE_BONUS = 0.04         # per extra matching line (max 2 extra)
SOURCE_BONUS = 0.05       # per extra independent observation that agrees (max 2 extra)
TERRAFORM_BONUS = 0.05    # structured Terraform parse agrees (+ again when it names the action)
CORROBORATION_MAX = 0.25  # retrieved runbook/incident for this category, weighted by rank and score
LLM_QUOTE_BONUS = 0.05    # per verbatim-verified LLM quote on top of a signature (max 2)
CONFLICT_PENALTY = 0.15   # a different signature also matches (ambiguous logs)
MAX_CONFIDENCE = 0.95     # never claim certainty

COST_BASE = 0.35
COST_Z_WEIGHT = 0.08      # per z-score unit above the threshold (capped)
COST_Z_MAX = 0.35
COST_HISTORY_MAX = 0.10   # more daily points -> a more trustworthy baseline

LLM_ONLY_BASE = 0.20      # LLM-only verdicts stay capped at 0.4 (README / agent.eval.LIVE_CONF_CAP)
LLM_ONLY_QUOTE = 0.05
LLM_ONLY_CAP = 0.4


def _corroboration(state: AgentState, category: str) -> float:
    """0..CORROBORATION_MAX. Each distinct corroborating source counts by its rank (1, .85, .7...)
    and by its score relative to the best hit; diminishing returns for a second source."""
    corroborating = CORROBORATING.get(category, set())
    sources = state.get("rag_sources", [])
    scores = state.get("rag_scores") or [1.0] * len(sources)
    top = max(scores, default=0.0) or 1.0
    seen, total = set(), 0.0
    for rank, (src, score) in enumerate(zip(sources, scores), start=1):
        base = os.path.basename(src)
        if base in corroborating and base not in seen:
            seen.add(base)
            weight = (score / top) * max(0.5, 1.0 - 0.15 * (rank - 1))
            total += weight * (1.0 if len(seen) == 1 else 0.5)
    return min(CORROBORATION_MAX, CORROBORATION_MAX * total)


def _cost_confidence(state: AgentState) -> float:
    cost = state.get("cost_anomaly") or {}
    z = cost.get("z_score")
    if not isinstance(z, (int, float)):
        return 0.0
    over = (COST_Z_MAX / COST_Z_WEIGHT) if math.isinf(z) else max(0.0, z - cost.get("threshold", 2.0))
    score = COST_BASE + min(COST_Z_MAX, COST_Z_WEIGHT * over)
    score += COST_HISTORY_MAX * min(1.0, max(0, cost.get("n_points", 0) - 7) / 23)
    return score


def calculate_confidence(state: AgentState) -> float:
    """0.0 if nothing is supported. Otherwise a graded score in (0, 0.95]: signature strength (lines,
    independent sources, Terraform parse), retrieval corroboration by rank/score, verified LLM quotes,
    minus a penalty when the logs also match a different signature. An LLM-only diagnosis
    (verbatim-verified evidence) is capped at 0.4. Mock/error observations never count."""
    category = state.get("category", UNKNOWN)
    obs = real_observations(state)
    evidence = state.get("evidence", [])
    if category == UNKNOWN or not obs or not evidence:
        return 0.0

    llm_quotes = sum(1 for e in evidence if e.startswith("log: "))
    if category == LLM_CATEGORY:
        return round(min(LLM_ONLY_CAP, LLM_ONLY_BASE + LLM_ONLY_QUOTE * llm_quotes), 2)
    if category == COST_ANOMALY:
        return round(min(MAX_CONFIDENCE, _cost_confidence(state)), 2)

    lines, sources, conflicts = 0, 0, set()
    for o in obs:
        found = signature_matches(o["content"])
        if category in found:
            sources += 1
            lines += len(found[category])
        conflicts |= set(found) - {category}

    score = SIGNATURE_BASE
    score += LINE_BONUS * min(2, max(0, lines - 1))
    score += SOURCE_BONUS * min(2, max(0, sources - 1))
    tf = state.get("terraform") or {}
    if tf:
        score += TERRAFORM_BONUS + (TERRAFORM_BONUS if tf.get("action") else 0.0)
    score += _corroboration(state, category)
    score += LLM_QUOTE_BONUS * min(2, llm_quotes)
    if conflicts:
        score -= CONFLICT_PENALTY
    return round(max(0.05, min(score, MAX_CONFIDENCE)), 2)
