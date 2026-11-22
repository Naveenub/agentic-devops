"""Pluggable chat-completions client: OpenAI, or any OpenAI-compatible server (e.g. a local LLaMA).

On only if OPENAI_API_KEY or OPENAI_BASE_URL is set; AGENT_LLM=off forces it off.
Every failure returns None so callers fall back to the deterministic path."""
import json
import os
from typing import Callable, Optional

_override: Optional[Callable[[str, str], Optional[str]]] = None


def set_client(fn: Optional[Callable[[str, str], Optional[str]]]) -> None:
    """Inject a (system, user) -> raw JSON string callable (tests); None restores default."""
    global _override
    _override = fn


def enabled() -> bool:
    if _override is not None:
        return True
    if os.getenv("AGENT_LLM", "").lower() == "off":
        return False
    return bool(os.getenv("OPENAI_API_KEY") or os.getenv("OPENAI_BASE_URL"))


def _http(system: str, user: str) -> Optional[str]:
    import httpx

    base = os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1").rstrip("/")
    headers = {"Content-Type": "application/json"}
    if os.getenv("OPENAI_API_KEY"):
        headers["Authorization"] = f"Bearer {os.environ['OPENAI_API_KEY']}"
    resp = httpx.post(
        f"{base}/chat/completions",
        headers=headers,
        timeout=float(os.getenv("AGENT_LLM_TIMEOUT", "30")),
        json={
            "model": os.getenv("AGENT_LLM_MODEL", "gpt-4o-mini"),
            "temperature": 0,
            "response_format": {"type": "json_object"},
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
        },
    )
    resp.raise_for_status()
    return resp.json()["choices"][0]["message"]["content"]


def complete_json(system: str, user: str) -> Optional[dict]:
    if not enabled():
        return None
    try:
        raw = _override(system, user) if _override else _http(system, user)
        data = json.loads(raw)
        return data if isinstance(data, dict) else None
    except Exception:
        return None
