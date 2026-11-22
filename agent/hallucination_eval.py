"""Unsupported-token ratio: share of the answer's words found in neither the retrieved
context nor the observations. Crude lexical metric, not a semantic groundedness check.

  python -m agent.hallucination_eval
"""
import json
import re
import sys
from pathlib import Path

from agent.eval import EVAL_DIR
from agent.memory.retrieval import retrieve
from agent.nodes.diagnose import build_query
from agent.orchestrator import run_agent

_WORD = re.compile(r"[a-z]+")
_STOP = {"the", "a", "an", "to", "of", "and", "or", "is", "was", "are", "were", "in", "on", "for",
         "due", "by", "it", "its", "that", "this", "with", "be", "as", "at", "no"}


def hallucination_score(answer: str, query: str, extra_context: str = "") -> float:
    """`query` is the retrieval query; `answer` should be the claim text (the diagnosis)."""
    context = " ".join([h.text for h in retrieve(query, n=3)] + [extra_context]).lower()
    support = set(_WORD.findall(context))
    answer_words = [w for w in _WORD.findall(answer.lower()) if w not in _STOP]
    if not answer_words:
        return 0.0
    unsupported = [w for w in answer_words if w not in support]
    return round(len(unsupported) / len(answer_words), 3)


def main() -> int:
    cases = json.loads((EVAL_DIR / "golden_incidents.json").read_text(encoding="utf-8"))
    limit = json.loads((EVAL_DIR / "expected_outputs.json").read_text(encoding="utf-8"))["max_unsupported_ratio"]
    status = 0
    for c in cases:
        obs = c.get("observations", [])
        state = {"question": c["query"], "observations": obs}
        claim = run_agent(state)["diagnosis"]  # templated suggestion text is not a claim
        score = hallucination_score(claim, build_query(state), " ".join(o["content"] for o in obs))
        ok = score <= limit
        status |= 0 if ok else 1
        print(f"{c['id']}: unsupported={score} limit={limit} {'PASS' if ok else 'FAIL'}")
    return status


if __name__ == "__main__":
    sys.exit(main())
