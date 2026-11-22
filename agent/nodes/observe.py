from agent.state import AgentState
from agent.tools.aws_costs import analyze_cost_anomaly
from agent.tools.github import GitHubToolError, get_ci_logs
from agent.tools.kubernetes import KubernetesToolError, get_pod_logs


def observe(state: AgentState) -> dict:
    """Collect raw observations (no reasoning). Keeps any pre-supplied observations."""
    observations = list(state.get("observations", []))

    k8s = state.get("kubernetes")
    if k8s:
        try:
            content = get_pod_logs(namespace=k8s.get("namespace", "default"), pod=k8s.get("pod"))
            observations.append({"source": "kubectl logs", "content": content})
        except KubernetesToolError as e:
            observations.append({"source": "kubectl logs", "content": str(e), "error": True})

    gh = state.get("github")
    if gh:
        try:
            jobs = get_ci_logs(repo=gh.get("repo", ""), run_id=str(gh.get("run_id", "")))
            observations += [{"source": f"github actions: {j['job']}", "content": j["content"]} for j in jobs]
        except GitHubToolError as e:
            observations.append({"source": "github actions", "content": str(e), "error": True})

    update = {"observations": observations}
    cost = state.get("cost")
    if cost:
        result = analyze_cost_anomaly(
            cost.get("service", ""), cost.get("region", ""),
            daily_costs=cost.get("daily_costs"), threshold=float(cost.get("threshold", 2.0)),
        )
        # No verdict (no data / Cost Explorer failure) is recorded as a tool error: never evidence.
        observations.append({"source": "aws cost explorer", "content": result["message"],
                             "error": result["anomaly"] is None})
        update["cost_anomaly"] = result
    return update
