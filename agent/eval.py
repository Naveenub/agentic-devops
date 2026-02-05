from agent.graph import agent
import json

def run_eval():
    tests = json.load(open("data/eval/golden_incidents.json"))

    for t in tests:
        result = agent.invoke({"question": t["question"]})
        diagnosis = result.get("diagnosis", "")

        if t["expected_root_cause"].lower() not in diagnosis.lower():
            print("❌ Hallucination detected")
        else:
            print("✅ Passed")

if __name__ == "__main__":
    run_eval()
