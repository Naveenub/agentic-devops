from agent.graph import build_graph

graph = build_graph()

while True:
    question = input("devops-ai> ")
    result = graph.invoke({"question": question})
    print(result["answer"])
    print("Confidence:", result["confidence"])
