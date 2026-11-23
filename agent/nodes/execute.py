from agent import approvals, executor
from agent.state import AgentState


def execute(state: AgentState) -> dict:
    """Executor stage. If the caller supplied a proposed file change (state["fix"]), queue it for human approval.
    This node never writes to GitHub: a PR is opened only by executor.execute_approved() after a human approves."""
    fix = state.get("fix")
    if not fix:
        return {}
    if not approvals.enabled():
        return {"fix_proposal": {"status": "unavailable", "error": "AGENT_APPROVALS_DB is not configured"}}
    try:
        stored = executor.propose(fix, requested_by=fix.get("requested_by") or "agent")
    except executor.ExecutorError as e:
        return {"fix_proposal": {"status": "rejected_input", "error": e.detail}}
    return {"fix_proposal": {"id": stored["id"], "status": stored["status"]}}
