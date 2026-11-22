from chromadb import PersistentClient

_client = PersistentClient(path=".chroma")
_collection = _client.get_or_create_collection("devops")


def query_knowledge(query: str, n_results: int = 3, where: dict | None = None):
    return _collection.query(
        query_texts=[query],
        n_results=n_results,
        where=where,
    )


def add_documents(documents, metadatas, ids):
    _collection.upsert(documents=documents, metadatas=metadatas, ids=ids)
