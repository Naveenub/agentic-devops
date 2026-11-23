"""Executor: the only agent stage that can cause a write, and only through the human-approval gate.

  propose()          validate one file change and store it as a pending proposal (writes nothing to GitHub)
  execute_approved() after a human approves: claim the proposal atomically, then open ONE pull request (never a merge)

Allow-list: AGENT_PR_REPOS (comma-separated owner/name). Empty allows nothing."""
import os

from agent import approvals
from agent.tools.github import GitHubToolError, _check_repo
from agent.tools import github_pr
from agent.tools.github_pr import MAX_CONTENT, check_path


class ExecutorError(Exception):
    """Carries the HTTP status the API should answer with (403 allow-list, 409 not pending, 422 bad input, 502 GitHub)."""

    def __init__(self, status: int, detail: str):
        super().__init__(detail)
        self.status, self.detail = status, detail


def allowed_repos() -> set[str]:
    return {r.strip() for r in os.getenv("AGENT_PR_REPOS", "").split(",") if r.strip()}


def label(value, default: str) -> str:
    return " ".join(str(value or default).split())[:60] or default


def propose(fix: dict, requested_by: str = "api") -> dict:
    """Validate and store a pending proposal. Raises ExecutorError."""
    repo = fix.get("repo")
    if repo not in allowed_repos():  # fail closed
        raise ExecutorError(403, "repo is not in AGENT_PR_REPOS")
    base, title, content = fix.get("base") or "main", fix.get("title"), fix.get("content")
    try:
        _check_repo(repo), check_path(fix.get("path"))
    except GitHubToolError as e:
        raise ExecutorError(422, str(e))
    if not isinstance(title, str) or not title.strip() or len(title) > 200:
        raise ExecutorError(422, "'title' is required (at most 200 characters)")
    if not isinstance(content, str) or len(content) > MAX_CONTENT:
        raise ExecutorError(422, f"'content' must be text of at most {MAX_CONTENT} characters")
    if not isinstance(base, str):
        raise ExecutorError(422, "'base' must be a branch name")
    return approvals.create(repo, base, fix["path"], content, title.strip(), str(fix.get("summary") or "")[:2000],
                            label(requested_by, "api"))


def execute_approved(fid: str, approver: str) -> dict:
    """Called only after the approver key was checked. Returns {"id","status","pr_url"}. Raises ExecutorError."""
    fix = approvals.get(fid)
    if not fix:
        raise ExecutorError(404, "no such fix")
    if fix["repo"] not in allowed_repos():  # the allow-list may have shrunk since the proposal was made
        raise ExecutorError(403, "repo is not in AGENT_PR_REPOS")
    who = label(approver, "approver")
    if not approvals.claim(fid, "approving", who):  # atomic: two approvals cannot both open a PR
        raise ExecutorError(409, "fix is not pending")
    try:
        pr = github_pr.open_fix_pr(fix["repo"], fix["base"], f"agent-fix/{fid}", fix["path"], fix["content"], fix["title"],
                         f"{fix['summary']}\n\n---\nProposed by {fix['requested_by']}, approved by {who}. "
                         "Opened by the DevOps AI Agent after human approval; review before merging.")
    except GitHubToolError as e:
        approvals.finish(fid, "failed", error=str(e))
        raise ExecutorError(502, str(e))
    approvals.finish(fid, "opened", pr_url=pr["url"])
    return {"id": fid, "status": "opened", "pr_url": pr["url"]}
