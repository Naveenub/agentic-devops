from agent.state import AgentState
from agent.tools.github import get_ci_logs
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
        ci = get_ci_logs(repo=gh.get("repo", ""), run_id=str(gh.get("run_id", "")))
        observations.append({"source": "github actions", **ci})

    return {"observations": observations}
