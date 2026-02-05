def route(query):
    if "cost" in query:
        return "cost"
    if "terraform" in query:
        return "diagnoser"
    return "observer"
