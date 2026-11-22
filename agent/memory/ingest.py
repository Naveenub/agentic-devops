# agent/memory/ingest.py

import os
import json

SUPPORTED_TEXT_EXTENSIONS = {".md", ".txt", ".log"}
SUPPORTED_JSON_EXTENSIONS = {".json"}

KNOWLEDGE_TYPES = {"runbook", "incident"}  # logs / eval / synthetic data must not enter the KB


def ingest_directory(base_path="data"):
    documents = []
    metadatas = []
    ids = []

    for root, _, files in os.walk(base_path):
        for file in files:
            path = os.path.join(root, file)
            ext = os.path.splitext(file)[1]

            if infer_type(path) not in KNOWLEDGE_TYPES:
                continue

            try:
                if ext in SUPPORTED_JSON_EXTENSIONS:
                    with open(path, "r", encoding="utf-8") as f:
                        content = json.dumps(json.load(f), indent=2)

                elif ext in SUPPORTED_TEXT_EXTENSIONS:
                    with open(path, "r", encoding="utf-8", errors="ignore") as f:
                        content = f.read()

                else:
                    continue  # skip unknown files

                documents.append(content)
                metadatas.append({
                    "source": path,
                    "type": infer_type(path)
                })
                ids.append(path)

            except Exception as e:
                print(f"⚠️ Skipping {path}: {e}")

    if documents:
        from agent.memory.chroma import add_documents  # lazy: keeps infer_type importable without chromadb

        add_documents(documents, metadatas, ids)
        print(f"✅ Ingested {len(documents)} documents")

def infer_type(path: str) -> str:
    if "synthetic" in path:  # check first: "synthetic/incidents.json" contains "incidents"
        return "synthetic"
    if "eval" in path:
        return "eval"
    if "runbooks" in path:
        return "runbook"
    if "incidents" in path:
        return "incident"
    if "logs" in path:
        return "log"
    return "unknown"

if __name__ == "__main__":
    ingest_directory()
