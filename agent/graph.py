from langgraph.graph import StateGraph
from agent.state import AgentState
from agent.nodes.observe import observe
from agent.nodes.diagnose import diagnose
from agent.nodes.suggest import suggest

def build_graph():
    graph = StateGraph(AgentState)

    graph.add_node("observe", observe)
    graph.add_node("diagnose", diagnose)
    graph.add_node("suggest", suggest)

    graph.set_entry_point("observe")
    graph.add_edge("observe", "diagnose")
    graph.add_edge("diagnose", "suggest")

    return graph.compile()
