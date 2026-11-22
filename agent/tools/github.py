"""GitHub Actions log access.

MOCK: no real GitHub integration exists yet. get_ci_logs returns clearly labelled
placeholder data (mock=True); downstream nodes must not use it as evidence.
TODO: replace with a read-only call to the Actions "download job logs" API.
"""

MOCK_LABEL = "[MOCK DATA - not fetched from GitHub]"


def get_ci_logs(repo: str, run_id: str) -> dict:
    return {
        "mock": True,
        "content": (
            f"{MOCK_LABEL}\n"
            f"repo={repo} run_id={run_id}\n"
            "Real CI log retrieval is not implemented."
        ),
    }
