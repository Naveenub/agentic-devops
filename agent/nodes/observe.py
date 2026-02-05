from agent.tools.kubernetes import get_pod_logs

def observe(state):
    if "kubernetes" in state.query or "pod" in state.query:
        state.logs = get_pod_logs("payments-api")
    return state
    
