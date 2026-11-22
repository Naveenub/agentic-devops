from langgraph.graph import END, START, StateGraph

from agent.orchestrator import PIPELINE
from agent.state import AgentState


def build_graph():
    graph = StateGraph(AgentState)
    for name, node in PIPELINE:
        graph.add_node(name, node)
    names = [name for name, _ in PIPELINE]
    graph.add_edge(START, names[0])
    for a, b in zip(names, names[1:]):
        graph.add_edge(a, b)
    graph.add_edge(names[-1], END)
    return graph.compile()
