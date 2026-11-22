"""Groundedness of each diagnosis against the retrieved context + observations.

  python -m agent.hallucination_eval            # offline: weighted lexical + specifics check
  python -m agent.hallucination_eval --judge    # live model's diagnoses + LLM per-claim entailment (needs an LLM)

A case fails if its weighted unsupported score exceeds `max_unsupported_ratio` OR it contains more
than `max_unsupported_specifics` specifics (numbers, ARNs, identifiers) absent from the context.
Details and limits: agent/groundedness.py. Cases with no diagnosis (abstentions) are skipped.
With --judge the diagnoses come from the real model (not llm_fixture) and offline_only cases are skipped;
a judge call that fails or returns garbage fails the run instead of silently scoring lexically.
"""
import json
import sys

from agent import llm
from agent.eval import EVAL_DIR, load_cases, run_case_agent
from agent.groundedness import check
from agent.memory.retrieval import retrieve
from agent.nodes.diagnose import build_query


def hallucination_report(answer: str, query: str, extra_context: str = "", judge: bool = False) -> dict:
    """`query` is the retrieval query; `answer` should be the claim text (the diagnosis)."""
    context = " ".join([h.text for h in retrieve(query, n=3)] + [extra_context])
    return check(answer, context, judge=judge)


def hallucination_score(answer: str, query: str, extra_context: str = "") -> float:
    return hallucination_report(answer, query, extra_context)["score"]


def main(argv=()) -> int:
    judge = "--judge" in argv
    limits = json.loads((EVAL_DIR / "expected_outputs.json").read_text(encoding="utf-8"))
    max_ratio, max_specifics = limits["max_unsupported_ratio"], limits.get("max_unsupported_specifics", 0)
    if judge and not llm.enabled():
        print("SKIP --judge: no LLM configured; running without it")
        judge = False
    status = scored = judge_failed = 0
    real = 0
    for c in load_cases():
        if judge and c.get("offline_only"):
            print(f"{c['id']}: SKIP (offline_only)")
            continue
        obs = c.get("observations", [])
        result = run_case_agent(c, live=judge)
        if result["diagnosis_source"] == "none":  # abstention text is not a claim
            print(f"{c['id']}: SKIP (abstained)")
            continue
        rep = hallucination_report(result["diagnosis"], build_query({"question": c["query"], "observations": obs}),
                                   " ".join(o["content"] for o in obs), judge=judge)
        if judge and rep["judge_unsupported_claims"] is None:
            judge_failed += 1
        bad_judge = rep["judge_unsupported_claims"] or []
        ok = rep["score"] <= max_ratio and len(rep["unsupported_specifics"]) <= max_specifics and not bad_judge
        status |= 0 if ok else 1
        scored += 1
        real += c["tier"] == "real"
        extra = f" specifics={rep['unsupported_specifics']}" if rep["unsupported_specifics"] else ""
        extra += f" judge_unsupported={bad_judge}" if bad_judge else ""
        print(f"{c['id']}: unsupported={rep['score']} limit={max_ratio} {'PASS' if ok else 'FAIL'}"
              f" source={result['diagnosis_source']}{extra}")
    print(f"scored {scored} diagnoses ({real} real)"
          + (" (live model, LLM judge)" if judge else " (fixtures, lexical + specifics only)"))
    if judge_failed:
        print(f"FAIL: LLM judge returned no verdict for {judge_failed} diagnoses; scores above are lexical only")
        status = 1
    if not real:
        print("WARNING: 0 real-incident diagnoses scored. Passing here is not evidence of real-world groundedness.")
    return status


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
