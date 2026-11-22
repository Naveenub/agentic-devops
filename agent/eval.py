"""Eval over data/eval/golden_incidents.json (offline by default).

  python -m agent.eval
Checks per case: retrieval grounding, diagnosed category, confidence_min, must_cite.
"""
import json
import sys
from pathlib import Path

from agent.memory.retrieval import retrieve
from agent.nodes.diagnose import build_query
from agent.orchestrator import run_agent

EVAL_DIR = Path(__file__).resolve().parents[1] / "data" / "eval"


def evaluate_hallucination(test_case: dict) -> dict:
    """Is retrieval, queried the way the agent queries it, grounded in the expected sources?"""
    expected = set(test_case.get("expected_sources", []))
    hits = retrieve(build_query({"question": test_case["query"],
                                 "observations": test_case.get("observations", [])}), n=5)

    if not hits:
        return {"grounded": False, "score": 0.0, "matched_sources": [], "reason": "No documents retrieved"}

    matched = sorted({h.source for h in hits} & expected)
    score = len(matched) / max(len(expected), 1)
    return {"grounded": score > 0.5, "score": round(score, 2), "matched_sources": matched}


def evaluate_case(case: dict, criteria: dict) -> dict:
    grounding = evaluate_hallucination(case)
    result = run_agent({"question": case["query"], "observations": case.get("observations", [])})
    checks = {
        "grounded": grounding["grounded"],
        "category": result["category"] == case["expected_category"],
        "confidence": result["confidence"] >= criteria["confidence_min"],
        "cited": (not criteria["must_cite"]) or any(e.startswith("retrieved: ") for e in result["evidence"]),
    }
    return {"id": case["id"], "passed": all(checks.values()), "checks": checks,
            "confidence": result["confidence"], "grounding": grounding}


def main() -> int:
    cases = json.loads((EVAL_DIR / "golden_incidents.json").read_text(encoding="utf-8"))
    criteria = json.loads((EVAL_DIR / "expected_outputs.json").read_text(encoding="utf-8"))
    rows = [evaluate_case(c, criteria) for c in cases]
    for r in rows:
        failed = [k for k, v in r["checks"].items() if not v]
        print(f"{r['id']}: {'PASS' if r['passed'] else 'FAIL ' + ','.join(failed)} confidence={r['confidence']}")
    return 0 if all(r["passed"] for r in rows) else 1


if __name__ == "__main__":
    sys.exit(main())
