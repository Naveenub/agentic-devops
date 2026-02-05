def kubectl_logs(pod):
    output = kubectl(f"logs {pod}")
    return {
        "tool": "kubectl logs",
        "resource": pod,
        "output": output
    }

state["observations"].append({
    "source": "kubectl logs",
    "content": logs
})

When making claims:
- Cite the tool or runbook used
- If no citation exists, say "Insufficient evidence"

{
  "diagnosis": "OOMKill detected",
  "evidence": [
    "kubectl logs: exit code 137",
    "runbook: eks-oomkill.md"
  ],
  "confidence": 0.86
}
