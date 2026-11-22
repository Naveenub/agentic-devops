# DevOps AI Agent – Demo Script

[0–10s]
This is an AI agent for real DevOps incidents, not a chatbot.

[10–25s]
We ask why the payments API pod is crashing in prod.

[25–45s]
The agent reads the Kubernetes logs and retrieves similar past incidents and runbooks using RAG.

[45–65s]
It identifies the root cause as a Pod OOMKill, citing the exact log line and retrieved sources.

[65–80s]
It proposes read-only checks and a safe fix through the normal pipeline, and a confidence score.
It never applies changes itself.

[80–90s]
Same flow for CI: a Terraform apply failure is parsed as a missing iam:PassRole permission.
