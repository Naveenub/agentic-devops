# agent/orchestrator.py

from agent.nodes.observe import observe
from agent.nodes.diagnose import diagnose
from agent.nodes.suggest import suggest

def run_agent(initial_state: dict) -> dict:
    """
    Runs the DevOps AI Agent pipeline:
    Observe → Diagnose → Suggest
    """

    state = initial_state.copy()

    # Step 1: Observe
    obs_result = observe(state)
    state.update(obs_result)

    # Step 2: Diagnose (RAG + reasoning happens here)
    diag_result = diagnose(state)
    state.update(diag_result)

    # Step 3: Suggest fixes
    suggestion = suggest(state)
    state.update(suggestion)

    return state
