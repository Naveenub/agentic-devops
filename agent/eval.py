"""Eval over data/eval/golden_incidents.json (hand-written) and data/eval/real/*.json (real incidents).

  python -m agent.eval                # offline + deterministic: signatures, retrieval, guardrails.
                                      # LLM behaviour is stubbed from each case's `llm_fixture`.
  python -m agent.eval --live         # real LLM (OPENAI_API_KEY / OPENAI_BASE_URL): measures LLM accuracy
  python -m agent.eval --min-real 20  # also fail unless >= 20 real-incident cases exist
                                      # (default: the number in data/eval/real/MIN_REAL, a ratchet: raise it as
                                      #  real cases are added, so the count can never silently shrink)

Offline mode proves the pipeline and its guardrails; it says nothing about LLM accuracy.
Only --live over real cases does, and the summary says how many real cases it covered.
Case schema: data/eval/real/README.md
"""
import argparse
import contextlib
import json
import math
import os
import sys
from collections import Counter
from pathlib import Path
from typing import List

from agent import llm
from agent.memory import retrieval
from agent.memory.retrieval import retrieve
from agent.nodes.diagnose import _DENY, build_query
from agent.orchestrator import run_agent
from agent.signatures import SIGNATURES, UNKNOWN
from agent.state import LLM_CATEGORY

EVAL_DIR = Path(__file__).resolve().parents[1] / "data" / "eval"
REQUIRED = ("id", "query", "expected_category")
VALID_CATEGORIES = {n for n, _ in SIGNATURES} | {"state_lock", LLM_CATEGORY, UNKNOWN}
LIVE_CONF_CAP = 0.4  # an LLM-only verdict never exceeds this (agent.confidence)


def load_cases(eval_dir=None, include_real: bool = True) -> List[dict]:
    d = Path(eval_dir) if eval_dir else EVAL_DIR
    cases = [{**c, "tier": "golden"} for c in json.loads((d / "golden_incidents.json").read_text(encoding="utf-8"))]
    if include_real:
        for p in sorted((d / "real").glob("*.json")):
            cases += [{**c, "tier": "real"} for c in json.loads(p.read_text(encoding="utf-8"))]
    ids = [c.get("id") for c in cases]
    dupes = sorted({i for i in ids if ids.count(i) > 1})
    if dupes:
        raise ValueError(f"duplicate eval case ids: {dupes}")
    for c in cases:
        missing = [k for k in REQUIRED if k not in c]
        if missing:
            raise ValueError(f"eval case {c.get('id', '?')} missing {missing}")
        if c["tier"] == "real" and c["expected_category"] not in VALID_CATEGORIES:
            raise ValueError(f"eval case {c['id']}: unknown expected_category {c['expected_category']!r}")
    return cases


def default_min_real(eval_dir=None) -> int:
    f = (Path(eval_dir) if eval_dir else EVAL_DIR) / "real" / "MIN_REAL"
    return int(f.read_text().strip()) if f.exists() else 0


def wilson(k: int, n: int, z: float = 1.96):
    """95% Wilson interval for k/n; with tiny n it is wide, which is the point."""
    if n == 0:
        return 0.0, 1.0
    p, d = k / n, 1 + z * z / n
    centre, margin = p + z * z / (2 * n), z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))
    return max(0.0, (centre - margin) / d), min(1.0, (centre + margin) / d)


def fmt_rate(k: int, n: int) -> str:
    lo, hi = wilson(k, n)
    return f"{k}/{n} ({k / max(n, 1):.0%}, 95% CI {lo:.0%}-{hi:.0%})"


def _norm(s: str) -> str:
    return " ".join(s.split()).lower()


def leakage(case: dict) -> List[str]:
    """A real case must not be answerable from the knowledge base (data/runbooks, data/incidents)."""
    docs, _ = retrieval._corpus()
    texts = [_norm(t) for _, t, _ in docs]
    problems = []
    if case.get("incident_id") and any(f'"{case["incident_id"]}"' in t.replace("'", '"') or
                                       _norm(case["incident_id"]) in t for t in texts):
        problems.append(f"incident_id {case['incident_id']} already in the knowledge base")
    for o in case.get("observations", []):
        for line in o.get("content", "").splitlines():
            line = _norm(line)
            if len(line) >= 30 and any(line in t for t in texts):
                problems.append(f"observation line already in the knowledge base: {line[:60]}")
                break
    return problems


@contextlib.contextmanager
def _llm_mode(case: dict, live: bool):
    prev_client, prev_env = llm._override, os.environ.get("AGENT_LLM")
    try:
        if live:
            llm.set_client(None)
        elif "llm_fixture" in case:
            payload = json.dumps(case["llm_fixture"])
            llm.set_client(lambda system, user: payload)
        else:
            llm.set_client(None)
            os.environ["AGENT_LLM"] = "off"
        yield
    finally:
        llm.set_client(prev_client)
        if prev_env is None:
            os.environ.pop("AGENT_LLM", None)
        else:
            os.environ["AGENT_LLM"] = prev_env


def run_case_agent(case: dict, live: bool = False) -> dict:
    """The one place a case is turned into an agent run (shared with hallucination_eval)."""
    with _llm_mode(case, live):
        return run_agent({"question": case["query"], "observations": case.get("observations", [])})


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


def evaluate_case(case: dict, criteria: dict, live: bool = False) -> dict:
    expected = case["expected_category"]
    result = run_case_agent(case, live)
    conf = result["confidence"]
    checks = {"category": result["category"] == expected,
              "safe_suggestions": not any(_DENY.search(s) for s in result["suggestions"])}
    grounding = None

    if expected == UNKNOWN:
        checks["abstained"] = conf <= (LIVE_CONF_CAP if live else 0.0)
        if live:  # a live model may legitimately propose something, but never confidently
            checks["category"] = result["category"] in (UNKNOWN, LLM_CATEGORY)
    elif expected == LLM_CATEGORY:
        checks["capped"] = conf <= LIVE_CONF_CAP
        checks["verified_evidence"] = any(e.startswith("log: ") for e in result["evidence"])
    else:
        grounding = evaluate_hallucination(case)
        checks["grounded"] = grounding["grounded"]
        checks["confidence"] = conf >= case.get("min_confidence", criteria["confidence_min"])
        checks["cited"] = (not criteria["must_cite"]) or any(e.startswith("retrieved: ") for e in result["evidence"])

    keywords = case.get("root_cause_keywords")
    if keywords:
        checks["root_cause"] = any(k.lower() in result["diagnosis"].lower() for k in keywords)

    return {"id": case["id"], "tier": case["tier"], "expected": expected, "passed": all(checks.values()),
            "checks": checks, "known_gap": case.get("known_gap"), "confidence": conf, "grounding": grounding, "diagnosis_source": result["diagnosis_source"]}


def main(argv=()) -> int:
    ap = argparse.ArgumentParser(prog="python -m agent.eval")
    ap.add_argument("--live", action="store_true", help="use the real LLM instead of fixtures")
    ap.add_argument("--min-real", type=int, default=None,
                    help="fail unless at least N real cases exist (default: data/eval/real/MIN_REAL)")
    args = ap.parse_args(list(argv))

    min_real = default_min_real() if args.min_real is None else args.min_real
    cases = load_cases()
    criteria = json.loads((EVAL_DIR / "expected_outputs.json").read_text(encoding="utf-8"))
    status = 0

    if args.live:
        if not llm.enabled():
            print("SKIP --live: no LLM configured (set OPENAI_API_KEY or OPENAI_BASE_URL)")
            return 0
        cases = [c for c in cases if not c.get("offline_only")]

    for c in (c for c in cases if c["tier"] == "real"):
        for problem in leakage(c):
            print(f"{c['id']}: LEAK {problem}")
            status = 1

    rows = [evaluate_case(c, criteria, live=args.live) for c in cases]
    for r in rows:
        failed = [k for k, v in r["checks"].items() if not v]
        if r["passed"] and r["known_gap"]:
            verdict, status = "XPASS (remove known_gap)", 1
        elif r["known_gap"]:
            verdict = f"XFAIL known gap: {r['known_gap']}"
        elif r["passed"]:
            verdict = "PASS"
        else:
            verdict, status = "FAIL " + ",".join(failed), 1
        print(f"{r['id']} [{r['tier']}]: {verdict} confidence={r['confidence']}")

    tiers = Counter(r["tier"] for r in rows)
    by_cat = Counter(r["expected"] for r in rows)
    gaps = sum(1 for r in rows if r["known_gap"] and not r["passed"])
    print(f"summary: {fmt_rate(sum(r['passed'] for r in rows), len(rows))} passed"
          + (f", {gaps} known gap(s) counted as misses" if gaps else "") + " | "
          + " ".join(f"{t}={n}" for t, n in sorted(tiers.items())) + " | "
          + " ".join(f"{k}={v}" for k, v in sorted(by_cat.items())))
    for t in sorted(tiers):
        print(f"  {t}: {fmt_rate(sum(r['passed'] for r in rows if r['tier'] == t), tiers[t])}")
    if args.live:
        llm_rows = [r for r in rows if r["diagnosis_source"] in ("llm", "signature+llm") or r["expected"] in (LLM_CATEGORY, UNKNOWN)]
        print(f"live LLM: {sum(r['passed'] for r in llm_rows)}/{len(llm_rows)} LLM-relevant cases passed "
              f"({tiers.get('real', 0)} real)")
    if tiers.get("real", 0) < min_real:
        print(f"FAIL: {tiers.get('real', 0)} real cases < --min-real {min_real}")
        status = 1
    if not tiers.get("real"):
        print("WARNING: 0 real-incident cases (data/eval/real/). Passing here is not evidence of real-world accuracy.")
    return status


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
