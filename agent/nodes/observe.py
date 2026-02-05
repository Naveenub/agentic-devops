from agent.tools.kubernetes import get_pod_logs

def observe(state):
    if "kubernetes" in state.query or "pod" in state.query:
        state.logs = get_pod_logs("payments-api")
    return state
    
from agent.tools.aws_costs import get_cost_breakdown

def observe_costs(state):
    if "cost" in state["question"].lower():
        costs = get_cost_breakdown()
        state["observations"].append({
            "source": "aws-cost-explorer",
            "content": costs
        })
    return state

def observe(logs):
    return "Detected CrashLoopBackOff from logs"
