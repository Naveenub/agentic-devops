"""Groundedness of each diagnosis against the retrieved context + observations.

  python -m agent.hallucination_eval            # offline: weighted lexical + specifics check
  python -m agent.hallucination_eval --judge    # also LLM per-claim entailment (needs an LLM)

A case fails if its weighted unsupported score exceeds `max_unsupported_ratio` OR it contains more
than `max_unsupported_specifics` specifics (numbers, ARNs, identifiers) absent from the context.
Details and limits: agent/groundedness.py. Cases with no diagnosis (abstentions) are skipped.
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
    status = scored = 0
    for c in load_cases():
        obs = c.get("observations", [])
        result = run_case_agent(c)
        if result["diagnosis_source"] == "none":  # abstention text is not a claim
            print(f"{c['id']}: SKIP (abstained)")
            continue
        rep = hallucination_report(result["diagnosis"], build_query({"question": c["query"], "observations": obs}),
                                   " ".join(o["content"] for o in obs), judge=judge)
        bad_judge = rep["judge_unsupported_claims"] or []
        ok = rep["score"] <= max_ratio and len(rep["unsupported_specifics"]) <= max_specifics and not bad_judge
        status |= 0 if ok else 1
        scored += 1
        extra = f" specifics={rep['unsupported_specifics']}" if rep["unsupported_specifics"] else ""
        extra += f" judge_unsupported={bad_judge}" if bad_judge else ""
        print(f"{c['id']}: unsupported={rep['score']} limit={max_ratio} {'PASS' if ok else 'FAIL'}"
              f" source={result['diagnosis_source']}{extra}")
    print(f"scored {scored} diagnoses" + (" (with LLM judge)" if judge else " (lexical + specifics only)"))
    return status


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
