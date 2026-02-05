from pathlib import Path
from agent.memory.chroma import collection

def ingest(path="data"):
    for file in Path(path).rglob("*.*"):
        collection.add(
            documents=[file.read_text()],
            metadatas=[{"source": str(file)}],
            ids=[str(file)]
        )

if __name__ == "__main__":
    ingest()
