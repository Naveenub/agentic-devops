"""Read-only GitHub Actions log access.

Two GET calls against the Actions API, nothing else:
  1. GET /repos/{repo}/actions/runs/{run_id}/jobs     -> find the failed jobs of the latest attempt
  2. GET /repos/{repo}/actions/jobs/{job_id}/logs     -> 302 to a signed blob URL, then plain-text logs

Needs GITHUB_TOKEN (fine-grained PAT or GitHub App token with `Actions: read`). Without it, or on any
API/network failure, GitHubToolError is raised and the observe node records an error observation
(never evidence). Logs are streamed and only the last `tail` lines per job are kept, because the
failure is at the end of a CI log.
"""
import os
import re
from collections import deque
from typing import List

import httpx

API = "https://api.github.com"
MAX_JOBS = 3              # failed jobs returned per run
TAIL_LINES = 200          # lines kept per job (from the end)
MAX_LINE_CHARS = 2000
MAX_JOB_CHARS = 20000

_FAILED = {"failure", "timed_out"}
_REPO = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
_RUN_ID = re.compile(r"^[0-9]{1,20}$")
_ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
_TIMESTAMP = re.compile(r"^\ufeff?\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?Z ")
_UNSAFE_NAME = re.compile(r"[^\w .()/-]")


class GitHubToolError(RuntimeError):
    pass


def _check_repo(repo) -> str:
    if (not isinstance(repo, str) or not _REPO.match(repo)
            or any(part in (".", "..") for part in repo.split("/"))):
        raise GitHubToolError(f"invalid repo (expected 'owner/name'): {repo!r}")
    return repo


def _check_run_id(run_id) -> str:
    run_id = str(run_id)
    if not _RUN_ID.match(run_id):
        raise GitHubToolError(f"invalid run_id (expected digits): {run_id!r}")
    return run_id


def _http_error(resp: httpx.Response, what: str) -> GitHubToolError:
    try:
        resp.read()
        detail = resp.json().get("message", "")
    except Exception:
        detail = ""
    hint = {
        401: "bad or expired GITHUB_TOKEN",
        403: "forbidden or rate limited (token needs Actions: read)",
        404: "not found, or the token cannot see this repo/run",
    }.get(resp.status_code, "")
    parts = [p for p in (hint, detail) if p]
    return GitHubToolError(f"GitHub {what} failed: HTTP {resp.status_code}" + (f" ({'; '.join(parts)})" if parts else ""))


def _clean(raw: str) -> str:
    return _TIMESTAMP.sub("", _ANSI.sub("", raw)).rstrip()[:MAX_LINE_CHARS]


def _tail(resp: httpx.Response, n: int) -> str:
    lines = deque(maxlen=n)
    for raw in resp.iter_lines():
        line = _clean(raw)
        if line:
            lines.append(line)
    return "\n".join(lines)[-MAX_JOB_CHARS:]


def _failed_jobs(client: httpx.Client, repo: str, run_id: str, headers: dict) -> List[dict]:
    resp = client.get(f"{API}/repos/{repo}/actions/runs/{run_id}/jobs",
                      headers=headers, params={"filter": "latest", "per_page": 100})
    if resp.status_code != 200:
        raise _http_error(resp, "job listing")
    jobs = [j for j in resp.json().get("jobs", []) if j.get("conclusion") in _FAILED]
    if not jobs:
        raise GitHubToolError(f"run {run_id} has no failed jobs (nothing to diagnose)")
    return jobs[:MAX_JOBS]


def _job_log_tail(client: httpx.Client, repo: str, job_id: int, headers: dict, tail: int) -> str:
    with client.stream("GET", f"{API}/repos/{repo}/actions/jobs/{int(job_id)}/logs", headers=headers) as resp:
        if resp.status_code == 200:
            return _tail(resp, tail)
        if resp.status_code != 302:
            raise _http_error(resp, "log download")
        location = resp.headers.get("location", "")
    if not location.startswith("https://"):
        raise GitHubToolError("GitHub log download returned an unusable redirect")
    # The signed URL is fetched WITHOUT the Authorization header: the token never leaves api.github.com.
    with client.stream("GET", location) as resp:
        if resp.status_code != 200:
            raise _http_error(resp, "log download")
        return _tail(resp, tail)


def get_ci_logs(repo: str, run_id: str, *, token: str = "", client: httpx.Client = None,
                tail: int = TAIL_LINES, timeout: float = 20.0) -> List[dict]:
    """Tail of the logs of each failed job (up to MAX_JOBS) of a workflow run.

    Returns [{"job": str, "conclusion": str, "content": str}, ...]. Raises GitHubToolError
    (bad input, no token, API/network failure, or no failed jobs). `client` is injectable for tests.
    """
    repo, run_id = _check_repo(repo), _check_run_id(run_id)
    token = token or os.getenv("GITHUB_TOKEN", "").strip()
    if not token:
        raise GitHubToolError("GITHUB_TOKEN is not set; cannot read GitHub Actions logs")
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json",
               "X-GitHub-Api-Version": "2022-11-28"}

    owned = client is None
    client = client or httpx.Client(timeout=timeout)
    try:
        out = []
        for job in _failed_jobs(client, repo, run_id, headers):
            name = " ".join(_UNSAFE_NAME.sub("", str(job.get("name", "job"))).split())[:60] or "job"
            out.append({"job": name, "conclusion": job["conclusion"],
                        "content": _job_log_tail(client, repo, job["id"], headers, tail)})
        return out
    except httpx.HTTPError as e:
        raise GitHubToolError(f"GitHub request failed: {type(e).__name__}: {e}") from e
    finally:
        if owned:
            client.close()
