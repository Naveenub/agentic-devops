from chromadb import Client
from chromadb.config import Settings

_client = Client(Settings(persist_directory=".chroma"))
_collection = _client.get_or_create_collection("devops")

def query_knowledge(query: str, n_results: int = 3, where: dict | None = None):
    results = _collection.query(
        query_texts=[query],
        n_results=n_results,
        where=where
    )

def add_documents(documents, metadatas, ids):
    _collection.add(
        documents=documents,
        metadatas=metadatas,
        ids=ids
    )
