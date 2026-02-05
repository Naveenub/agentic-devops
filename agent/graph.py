from langgraph.graph import StateGraph
from agent.state import AgentState
from agent.nodes.observe import observe
from agent.nodes.diagnose import diagnose
from agent.nodes.suggest import suggest

def build_graph():
    graph = StateGraph(AgentState)

graph.add_node("observer", observe_k8s)
graph.add_node("diagnoser", diagnose_with_rag)
graph.add_node("suggestor", suggest_fix)

graph.set_entry_point("observer")
graph.add_edge("observer", "diagnoser")
graph.add_edge("diagnoser", "suggestor")

    return graph.compile()
