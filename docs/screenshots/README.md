# Screenshots – DevOps AI Agent

Rendered from the real output of `agent.orchestrator.run_agent` on the sample logs in
`data/logs/` (offline retriever, no LLM key). Terminal-style renders, not live captures.

| File | Shows |
|---|---|
| 01-incident-input.png | The question and the payments-api log lines the agent observes |
| 02-agent-analysis.png | Observation plus RAG retrieval of runbooks / past incidents, with scores |
| 03-root-cause.png | Root cause, evidence and read-only suggested fix (OOMKill) |
| 04-confidence-score.png | Heuristic confidence score (not a calibrated probability) |
| 05-terraform-failure.png | Terraform CI failure parsed as an IAM `iam:PassRole` denial |
