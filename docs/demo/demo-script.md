# DevOps AI Agent – 90 Second Demo Script

[0–10s]
This is an AI agent designed for real DevOps incidents — not a chatbot.

[10–25s]
An alert comes in through Slack: the payments API pods are restarting.

[25–40s]
The agent fetches Kubernetes logs and retrieves similar past incidents and runbooks using RAG.

[40–55s]
It identifies the root cause as a Pod OOMKill and explains why, citing exact sources.

[55–65s]
A confidence score is generated. If this were low, PagerDuty escalation would trigger automatically.

[65–80s]
In parallel, the agent detects Terraform drift and suggests an auto-generated fix via pull request.

[80–90s]
This isn’t AI guessing — it’s AI reasoning over your infrastructure.
