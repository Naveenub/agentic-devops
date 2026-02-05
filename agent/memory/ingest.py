# agent/memory/ingest.py

import os
import json
from agent.memory.chroma import add_documents

SUPPORTED_TEXT_EXTENSIONS = {".md", ".txt", ".log"}
SUPPORTED_JSON_EXTENSIONS = {".json"}

def ingest_directory(base_path="data"):
    documents = []
    metadatas = []
    ids = []

    for root, _, files in os.walk(base_path):
        for file in files:
            path = os.path.join(root, file)
            ext = os.path.splitext(file)[1]

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
        add_documents(documents, metadatas, ids)
        print(f"✅ Ingested {len(documents)} documents")

def infer_type(path: str) -> str:
    if "runbooks" in path:
        return "runbook"
    if "incidents" in path:
        return "incident"
    if "logs" in path:
        return "log"
    if "eval" in path:
        return "eval"
    return "unknown"

if __name__ == "__main__":
    ingest_directory()
