from chromadb import Client
from chromadb.config import Settings

_client = Client(Settings(persist_directory=".chroma"))
_collection = _client.get_or_create_collection("devops")

def get_collection():
    return _collection

def query_knowledge(query: str, n_results: int = 3):
    results = _collection.query(
        query_texts=[query],
        n_results=n_results
    )
    return results
