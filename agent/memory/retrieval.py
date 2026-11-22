"""Retrieval behind one interface. Default backend is local/offline and deterministic
(no embedding download); set AGENT_RETRIEVER=chroma to use the Chroma store."""
import json
import math
import os
import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import List

REPO_ROOT = Path(__file__).resolve().parents[2]
KNOWLEDGE_DIRS = ("data/runbooks", "data/incidents")  # NOT logs / eval / synthetic

_TOKEN = re.compile(r"[a-z0-9]+")
_STOP = {"the", "and", "for", "with", "was", "are", "not", "has", "had", "this", "that", "from", "pod"}


@dataclass(frozen=True)
class Hit:
    source: str  # repo-relative path
    text: str
    score: float


def _tokens(text: str) -> set:
    return {t for t in _TOKEN.findall(text.lower()) if len(t) >= 3 and t not in _STOP}


@lru_cache(maxsize=1)
def _corpus(root: str = str(REPO_ROOT)):
    docs = []
    for rel in KNOWLEDGE_DIRS:
        for path in sorted((Path(root) / rel).glob("*")):
            if path.suffix == ".json":
                text = json.dumps(json.loads(path.read_text(encoding="utf-8")), indent=2)
            elif path.suffix == ".md":
                text = path.read_text(encoding="utf-8")
            else:
                continue
            docs.append((path.relative_to(root).as_posix(), text, _tokens(text)))
    df = {}
    for _, _, toks in docs:
        for t in toks:
            df[t] = df.get(t, 0) + 1
    return docs, df


def _local(query: str, n: int) -> List[Hit]:
    docs, df = _corpus()
    q = _tokens(query)
    hits = []
    for source, text, toks in docs:
        score = sum(math.log(1 + len(docs) / df[t]) for t in q & toks)
        if score > 0:
            hits.append(Hit(source, text, round(score, 4)))
    hits.sort(key=lambda h: (-h.score, h.source))
    return hits[:n]


def _chroma(query: str, n: int) -> List[Hit]:
    from agent.memory.chroma import query_knowledge

    res = query_knowledge(query, n_results=n)
    docs = res.get("documents", [[]])[0]
    metas = res.get("metadatas", [[]])[0]
    dists = res.get("distances", [[0.0] * len(docs)])[0]
    return [Hit(m.get("source", "unknown"), d, round(1 - x, 4)) for d, m, x in zip(docs, metas, dists)]


def retrieve(query: str, n: int = 3) -> List[Hit]:
    if os.getenv("AGENT_RETRIEVER", "local") == "chroma":
        return _chroma(query, n)
    return _local(query, n)
