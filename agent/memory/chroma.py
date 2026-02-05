from chromadb import Client
from chromadb.config import Settings

client = Client(Settings(persist_directory=".chroma"))
collection = client.get_or_create_collection("devops")

def query_knowledge(query: str):
    return collection.query(query_texts=[query], n_results=3)["metadatas"]
