"""Human-in-the-loop store for proposed fixes. A proposal does nothing until a human with the approver key
approves it; only then is a pull request opened (agent/tools/github_pr.py). SQLite, stdlib only.

  AGENT_APPROVALS_DB   path of the SQLite file. Unset = the /fixes routes answer 503.

Status: pending -> approving -> opened | failed;  pending -> rejected. Each transition is one conditional UPDATE,
so two concurrent approvals cannot both open a PR."""
import os
import secrets
import sqlite3
import time
from contextlib import contextmanager

SCHEMA = """CREATE TABLE IF NOT EXISTS fixes (
  id TEXT PRIMARY KEY, created_at REAL NOT NULL, status TEXT NOT NULL DEFAULT 'pending',
  repo TEXT NOT NULL, base TEXT NOT NULL, path TEXT NOT NULL, content TEXT NOT NULL, title TEXT NOT NULL,
  summary TEXT NOT NULL, requested_by TEXT NOT NULL, decided_by TEXT, decided_at REAL, pr_url TEXT, error TEXT)"""


def enabled() -> bool:
    return bool(os.getenv("AGENT_APPROVALS_DB"))


@contextmanager
def _conn():
    c = sqlite3.connect(os.environ["AGENT_APPROVALS_DB"], timeout=10)
    c.row_factory = sqlite3.Row
    try:
        c.execute(SCHEMA)
        yield c
        c.commit()
    finally:
        c.close()


def create(repo: str, base: str, path: str, content: str, title: str, summary: str, requested_by: str) -> dict:
    fid = secrets.token_hex(6)
    with _conn() as c:
        c.execute("INSERT INTO fixes(id,created_at,repo,base,path,content,title,summary,requested_by) VALUES(?,?,?,?,?,?,?,?,?)",
                  (fid, time.time(), repo, base, path, content, title, summary, requested_by))
    return get(fid)


def get(fid: str) -> dict | None:
    with _conn() as c:
        r = c.execute("SELECT * FROM fixes WHERE id=?", (fid,)).fetchone()
    return dict(r) if r else None


def pending() -> list[dict]:
    with _conn() as c:
        rows = c.execute("SELECT id,created_at,repo,base,path,title,requested_by FROM fixes "
                         "WHERE status='pending' ORDER BY created_at").fetchall()
    return [dict(r) for r in rows]


def claim(fid: str, to: str, by: str) -> bool:
    """pending -> `to` ('approving' or 'rejected'), recording who decided. False if it was not pending."""
    with _conn() as c:
        return c.execute("UPDATE fixes SET status=?, decided_by=?, decided_at=? WHERE id=? AND status='pending'",
                         (to, by, time.time(), fid)).rowcount == 1


def finish(fid: str, status: str, pr_url: str | None = None, error: str | None = None) -> None:
    with _conn() as c:
        c.execute("UPDATE fixes SET status=?, pr_url=?, error=? WHERE id=? AND status='approving'",
                  (status, pr_url, error, fid))
