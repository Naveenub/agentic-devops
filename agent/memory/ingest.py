# agents/memory/ingest.py
import os
from rag.vectorstore import get_collection

collection = get_collection()

SUPPORTED_EXTENSIONS = {".md", ".txt", ".log"}

def ingest_directory(path: str):
    for root, _, files in os.walk(path):
        for file in files:
            if os.path.splitext(file)[1] in SUPPORTED_EXTENSIONS:
                full_path = os.path.join(root, file)
                ingest_file(full_path)

def ingest_file(filepath: str):
    with open(filepath, "r", encoding="utf-8", errors="ignore") as f:
        content = f.read()

    collection.add(
        documents=[content],
        metadatas=[{"source": filepath}],
        ids=[filepath]
    )

    print(f"✅ Ingested: {filepath}")

if __name__ == "__main__":
    ingest_directory("data/")
