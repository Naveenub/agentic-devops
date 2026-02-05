from agent.memory.chroma import query_knowledge

def diagnose(state):
    docs = query_knowledge(state.query)
    state.diagnosis = "Pod likely OOMKilled based on historical incidents."
    state.sources = [d["source"] for d in docs]
    return state

def diagnose(context):
    return "Likely OOMKilled based on prior incidents"
