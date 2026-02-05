from agent.graph import build_graph
from agent.state import AgentState

graph = build_graph()

while True:
    query = input("devops-ai> ")
    state = AgentState(query=query)
    result = graph.invoke(state)
    print(result.answer)
    print("Confidence:", result.confidence)
