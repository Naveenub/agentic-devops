import os
import json
from chromadb import Client
from chromadb.utils import embedding_functions
from pathlib import Path

DATA_DIR = "data"

def ingest():
    client = Client()
    collection = client.get_or_create_collection(
        name="devops-memory",
        embedding_function=embedding_functions.DefaultEmbeddingFunction()
    )

    docs = []

    for root, _, files in os.walk(DATA_DIR):
        for f in files:
            path = os.path.join(root, f)
            if f.endswith(".md"):
                docs.append(open(path).read())
            elif f.endswith(".json"):
                docs.append(json.dumps(json.load(open(path))))

    for i, doc in enumerate(docs):
        collection.add(
            documents=[doc],
            ids=[f"doc-{i}"]
        )

    print(f"Ingested {len(docs)} documents into Chroma")

def ingest_dir(path):
    for file in Path(path).rglob("*.*"):
        collection.add(
            documents=[file.read_text()],
            metadatas=[{"source": str(file)}],
            ids=[str(file)]
        )

ingest_dir("data/runbooks")
ingest_dir("data/incidents")
ingest_dir("data/logs")

if __name__ == "__main__":
    ingest()
