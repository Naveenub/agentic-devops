"""Open a pull request that changes ONE file. The only write path to GitHub in this repo, and it is only
reachable through an approved proposal (api/main.py POST /fixes/{id}/approve).

Never merges, never pushes to the base branch, never touches .github/ (workflows) or .git/. Needs GITHUB_PR_TOKEN,
a separate fine-grained token from the read-only GITHUB_TOKEN (Contents: write + Pull requests: write on the
allow-listed repos only). Calls: GET base ref, POST ref, GET/PUT contents, POST pulls; the new branch is deleted
again if a later step fails."""
import base64
import os
import re

import httpx

from agent.tools.github import API, GitHubToolError, _check_repo

MAX_CONTENT = 100_000
_BRANCH = re.compile(r"^agent-fix/[a-z0-9-]{1,40}$")
_BASE = re.compile(r"^[\w.-]+(/[\w.-]+)*$")
_PATH = re.compile(r"^[\w.@+-]+(/[\w.@+-]+)*$")


def check_path(path) -> str:
    """Relative repo path of a normal file. Refuses traversal and CI/git internals (a PR that edits workflows is a
    way to run code with the repo's secrets)."""
    if (not isinstance(path, str) or len(path) > 200 or not _PATH.match(path)
            or any(p in (".", "..") for p in path.split("/")) or path.split("/")[0].lower() in (".git", ".github")):
        raise GitHubToolError(f"invalid path (relative file path, not under .git or .github): {path!r}")
    return path


def _fail(resp: httpx.Response, what: str) -> GitHubToolError:
    try:
        detail = str(resp.json().get("message", ""))[:200]
    except Exception:
        detail = ""
    return GitHubToolError(f"GitHub {what} failed: HTTP {resp.status_code}" + (f" ({detail})" if detail else ""))


def open_fix_pr(repo: str, base: str, branch: str, path: str, content: str, title: str, body: str, *,
                token: str = "", client: httpx.Client = None, timeout: float = 20.0) -> dict:
    """Returns {"url", "number"}. Raises GitHubToolError on bad input, no token, or any API/network failure."""
    repo, path = _check_repo(repo), check_path(path)
    if not isinstance(base, str) or not _BASE.match(base) or ".." in base:
        raise GitHubToolError(f"invalid base branch: {base!r}")
    if not isinstance(branch, str) or not _BRANCH.match(branch):
        raise GitHubToolError(f"invalid branch (expected agent-fix/<id>): {branch!r}")
    if not isinstance(content, str) or len(content) > MAX_CONTENT:
        raise GitHubToolError(f"content must be text of at most {MAX_CONTENT} characters")
    token = token or os.getenv("GITHUB_PR_TOKEN", "").strip()
    if not token:
        raise GitHubToolError("GITHUB_PR_TOKEN is not set; cannot open pull requests")
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json",
               "X-GitHub-Api-Version": "2022-11-28"}
    owned = client is None
    client = client or httpx.Client(timeout=timeout)

    def call(method, url, **kw):
        return client.request(method, f"{API}/repos/{repo}{url}", headers=headers, **kw)

    created = False
    try:
        r = call("GET", f"/git/ref/heads/{base}")
        if r.status_code != 200:
            raise _fail(r, "base branch lookup")
        r = call("POST", "/git/refs", json={"ref": f"refs/heads/{branch}", "sha": r.json()["object"]["sha"]})
        if r.status_code != 201:
            raise _fail(r, "branch creation")
        created = True
        r = call("GET", f"/contents/{path}", params={"ref": branch})
        if r.status_code not in (200, 404):
            raise _fail(r, "file lookup")
        put = {"message": title[:200], "content": base64.b64encode(content.encode()).decode(), "branch": branch}
        if r.status_code == 200 and isinstance(r.json(), dict) and r.json().get("sha"):
            put["sha"] = r.json()["sha"]
        r = call("PUT", f"/contents/{path}", json=put)
        if r.status_code not in (200, 201):
            raise _fail(r, "commit")
        r = call("POST", "/pulls", json={"title": title[:200], "head": branch, "base": base, "body": body})
        if r.status_code != 201:
            raise _fail(r, "pull request creation")
        created = False  # the PR now holds the branch
        pr = r.json()
        return {"url": pr["html_url"], "number": pr["number"]}
    except httpx.HTTPError as e:
        raise GitHubToolError(f"GitHub request failed: {type(e).__name__}: {e}") from e
    finally:
        if created:
            try:
                call("DELETE", f"/git/refs/heads/{branch}")
            except httpx.HTTPError:
                pass
        if owned:
            client.close()
