"""Single source of truth for the pipeline; graph.py builds from the same list."""
from agent.nodes.diagnose import diagnose
from agent.nodes.execute import execute
from agent.nodes.observe import observe
from agent.nodes.suggest import suggest
from agent.state import AgentState

PIPELINE = [("observer", observe), ("diagnoser", diagnose), ("suggestor", suggest), ("executor", execute)]


def run_agent(initial_state: AgentState) -> AgentState:
    """Observe -> Diagnose -> Suggest -> Execute (queue for approval), without LangGraph. Nodes return partial updates."""
    state: AgentState = dict(initial_state)
    for _, node in PIPELINE:
        state.update(node(state))
    return state
