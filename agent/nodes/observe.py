# agent/nodes/observe.py

from agent.tools.kubernetes import get_pod_logs
from agent.tools.github import get_ci_logs

def observe(state):
    """
    Collect raw system observations (no reasoning, no RAG).
    """

    observations = {}

    if state.get("kubernetes"):
        observations["kubernetes_logs"] = get_pod_logs(
            namespace=state["kubernetes"].get("namespace", "default"),
            pod=state["kubernetes"].get("pod")
        )

    if state.get("github"):
        observations["ci_logs"] = get_ci_logs(
            repo=state["github"]["repo"],
            run_id=state["github"]["run_id"]
        )

    return {
        "observations": observations
    }
