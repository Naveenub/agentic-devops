# Real-incident eval set (step 4)

Put **real, sanitized** incidents here as `data/eval/real/*.json` (each file is a JSON list of cases).
`python -m agent.eval` loads them automatically and reports golden vs real counts separately.
Until this directory has cases, eval passing is **not** evidence of real-world accuracy
(`python -m agent.eval --min-real 20` makes CI fail until it does).

## Adding a case

```bash
python -m agent.real_case --id REAL-001 --query "payments-api pods restarting" \
  --source "kubectl logs" --log raw.log --category oom_killed \
  --keyword memory --expected-source data/runbooks/eks-oomkill.md \
  --out data/eval/real/incidents.json
```

It redacts account IDs, IPs, emails, keys/tokens and EC2 hostnames (best-effort: read the result), appends the
case, and refuses bad categories, duplicate ids and missing keywords. `--category` comes from the postmortem.
Then run `python -m agent.eval`: the leakage check runs on the new case.

## Ratchet

`data/eval/real/MIN_REAL` holds the minimum real-case count that `python -m agent.eval` (and so CI) enforces.
It is `0` today, so CI passes with a warning. Raise it as cases land (e.g. `15`) so the set cannot shrink.

## Case schema

```json
{
  "id": "REAL-001",
  "incident_id": "INC-2025-03-...",            // optional; checked against the knowledge base for leakage
  "query": "what the on-call actually asked",
  "observations": [{"source": "kubectl logs", "content": "<verbatim, sanitized log excerpt>"}],
  "expected_category": "oom_killed",           // signature category, "other_llm" (no signature, LLM must find it), or "unknown"
  "root_cause_keywords": ["memory"],           // any one must appear in the diagnosis; use the postmortem's wording
  "expected_sources": ["data/runbooks/eks-oomkill.md"],   // only for signature categories
  "min_confidence": 0.6                        // optional override
}
```

## Rules that keep the numbers honest

- **Verbatim logs.** Paste what the agent would really have seen, not a cleaned-up version. Include the noise.
- **Label from the postmortem**, not from what the agent says. Labelling after seeing agent output inflates accuracy.
- **No leakage.** A case whose `incident_id` or log lines already appear in `data/runbooks` / `data/incidents`
  is rejected by the loader's leakage check (the retrieval step would just hand back the answer).
  Add the incident to the knowledge base only *after* it is out of the eval set (or split by date).
- **Include hard negatives**: real incidents the agent should abstain on (`"unknown"`), and incidents whose
  cause is NOT one of the four signatures (`"other_llm"`) - those are where LLM accuracy shows.
- **Sanitize**: strip account IDs, hostnames, tokens, customer data before committing.
- Fixture-only fields (`llm_fixture`, `offline_only`) belong to hand-written guardrail cases; leave them off real cases.

## Measuring the LLM

```bash
OPENAI_API_KEY=... python -m agent.eval --live            # accuracy of the real model on all non-offline cases
OPENAI_API_KEY=... python -m agent.hallucination_eval --judge   # + per-claim entailment judge
```
