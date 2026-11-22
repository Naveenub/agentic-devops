import hashlib
import os
import re

import numpy as np
from chromadb import EmbeddingFunction, PersistentClient

_DIM = 256
_TOKEN = re.compile(r"[a-z0-9]+")


class HashEmbedding(EmbeddingFunction):
    """Deterministic, offline bag-of-words hashing embedding (no model download).
    Select with CHROMA_EMBEDDER=hash; default is Chroma's built-in embedder."""

    def __init__(self):
        pass

    def __call__(self, input):
        out = []
        for text in input:
            v = np.zeros(_DIM, dtype=np.float32)
            for tok in _TOKEN.findall(text.lower()):
                v[int(hashlib.md5(tok.encode()).hexdigest(), 16) % _DIM] += 1.0
            n = np.linalg.norm(v)
            out.append(v / n if n else v)
        return out

    @staticmethod
    def name() -> str:
        return "agent-hash-embedding"

    def get_config(self):
        return {}

    @staticmethod
    def build_from_config(config):
        return HashEmbedding()


def _embedder():
    return HashEmbedding() if os.getenv("CHROMA_EMBEDDER") == "hash" else None


_client = PersistentClient(path=os.getenv("CHROMA_PATH", ".chroma"))
_kwargs = {"embedding_function": _embedder()} if _embedder() else {}
_collection = _client.get_or_create_collection("devops", **_kwargs)


def query_knowledge(query: str, n_results: int = 3, where: dict | None = None):
    return _collection.query(query_texts=[query], n_results=n_results, where=where)


def add_documents(documents, metadatas, ids):
    _collection.upsert(documents=documents, metadatas=metadatas, ids=ids)
