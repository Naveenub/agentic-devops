import base64
import json

import httpx
import pytest
from fastapi.testclient import TestClient

from agent.tools.github import GitHubToolError
from agent.tools.github_pr import open_fix_pr
from api.main import app

KEY, APPROVER = {"x-api-key": "k"}, {"x-approver-key": "a", "x-approver": "dana"}
FIX = {"repo": "o/r", "path": "infra/iam.tf", "content": "resource {}\n", "title": "Grant iam:PassRole", "summary": "why"}


def gh(calls, fail_at=None, existing_sha=None):
    """Fake GitHub: records (method, path, json) and answers like the real API."""
    def handler(req: httpx.Request) -> httpx.Response:
        body = json.loads(req.content) if req.content else None
        calls.append((req.method, req.url.path, body, req.headers.get("authorization")))
        step = f"{req.method} {req.url.path.split('/repos/o/r')[1]}"
        if fail_at and step.startswith(fail_at):
            return httpx.Response(422, json={"message": "nope"})
        if step == "GET /git/ref/heads/main":
            return httpx.Response(200, json={"object": {"sha": "base123"}})
        if step.startswith("POST /git/refs"):
            return httpx.Response(201, json={})
        if step.startswith("GET /contents/"):
            return httpx.Response(200, json={"sha": existing_sha}) if existing_sha else httpx.Response(404, json={})
        if step.startswith("PUT /contents/"):
            return httpx.Response(201, json={})
        if step == "POST /pulls":
            return httpx.Response(201, json={"html_url": "https://github.com/o/r/pull/7", "number": 7})
        return httpx.Response(204)
    return httpx.Client(transport=httpx.MockTransport(handler))


def pr(client, **kw):
    args = dict(repo="o/r", base="main", branch="agent-fix/ab12", path="infra/iam.tf", content="x", title="t", body="b",
                token="tok", client=client)
    return open_fix_pr(**{**args, **kw})


def test_pr_flow_creates_branch_commits_one_file_and_opens_a_pr_without_merging():
    calls = []
    out = pr(gh(calls), content="hello")
    assert out == {"url": "https://github.com/o/r/pull/7", "number": 7}
    steps = [(m, p.split("/repos/o/r")[1]) for m, p, _, _ in calls]
    assert steps == [("GET", "/git/ref/heads/main"), ("POST", "/git/refs"), ("GET", "/contents/infra/iam.tf"),
                     ("PUT", "/contents/infra/iam.tf"), ("POST", "/pulls")]
    put = calls[3][2]
    assert base64.b64decode(put["content"]) == b"hello" and put["branch"] == "agent-fix/ab12" and "sha" not in put
    assert calls[1][2] == {"ref": "refs/heads/agent-fix/ab12", "sha": "base123"}
    assert all(c[3] == "Bearer tok" for c in calls) and not any(m in ("DELETE", "PATCH") for m, *_ in calls)


def test_updating_an_existing_file_sends_its_sha():
    calls = []
    pr(gh(calls, existing_sha="old9"))
    assert calls[3][2]["sha"] == "old9"


def test_a_failure_after_the_branch_exists_deletes_the_branch():
    calls = []
    with pytest.raises(GitHubToolError, match="commit failed: HTTP 422"):
        pr(gh(calls, fail_at="PUT /contents"))
    assert calls[-1][0] == "DELETE" and calls[-1][1].endswith("/git/refs/heads/agent-fix/ab12")


@pytest.mark.parametrize("bad", ["../x", "/etc/x", ".github/workflows/ci.yml", ".git/config", "a/../b", "a b", "", None,
                                 ".GitHub/x"])
def test_unsafe_paths_never_reach_github(bad):
    calls = []
    with pytest.raises(GitHubToolError):
        pr(gh(calls), path=bad)
    assert calls == []


def test_bad_branch_base_token_and_size_are_refused(monkeypatch):
    monkeypatch.delenv("GITHUB_PR_TOKEN", raising=False)
    for kw in ({"branch": "main"}, {"branch": "agent-fix/../x"}, {"base": "a..b"}, {"token": "", "client": gh([])},
               {"content": "x" * 100_001}):
        with pytest.raises(GitHubToolError):
            pr(kw.pop("client", gh([])), **kw)


@pytest.fixture
def api(tmp_path, monkeypatch):
    monkeypatch.delenv("PRODUCT_DB", raising=False)
    monkeypatch.setenv("AGENT_API_KEY", "k")
    monkeypatch.setenv("AGENT_APPROVER_KEY", "a")
    monkeypatch.setenv("AGENT_APPROVALS_DB", str(tmp_path / "a.db"))
    monkeypatch.setenv("AGENT_PR_REPOS", "o/r")
    calls = []
    monkeypatch.setattr("agent.tools.github_pr.open_fix_pr",
                        lambda *a, **k: open_fix_pr(*a, token="tok", client=gh(calls), **k))
    return TestClient(app), calls


def test_nothing_is_opened_until_a_human_approves(api):
    c, calls = api
    fid = c.post("/fixes", json=FIX, headers=KEY).json()["id"]
    assert calls == []
    assert [f["id"] for f in c.get("/fixes", headers=KEY).json()["pending"]] == [fid]
    assert c.get(f"/fixes/{fid}", headers=KEY).json()["content"] == FIX["content"]  # the approver reviews the real diff
    r = c.post(f"/fixes/{fid}/approve", headers=APPROVER)
    assert r.status_code == 200 and r.json() == {"id": fid, "status": "opened", "pr_url": "https://github.com/o/r/pull/7"}
    pr_body = [b for m, p, b, _ in calls if p.endswith("/pulls")][0]
    assert "approved by dana" in pr_body["body"] and pr_body["head"] == f"agent-fix/{fid}"
    assert c.get(f"/fixes/{fid}", headers=KEY).json()["pr_url"] == "https://github.com/o/r/pull/7"
    assert c.post(f"/fixes/{fid}/approve", headers=APPROVER).status_code == 409  # never a second PR
    assert len([1 for m, p, *_ in calls if p.endswith("/pulls")]) == 1


def test_requester_key_cannot_approve_and_keys_must_differ(api, monkeypatch):
    c, calls = api
    fid = c.post("/fixes", json=FIX, headers=KEY).json()["id"]
    assert c.post(f"/fixes/{fid}/approve", headers=KEY).status_code == 401
    assert c.post(f"/fixes/{fid}/approve").status_code == 401
    monkeypatch.setenv("AGENT_APPROVER_KEY", "k")  # same secret as the requester: refuse rather than self-approve
    assert c.post(f"/fixes/{fid}/approve", headers={"x-approver-key": "k"}).status_code == 503
    assert calls == []


def test_reject_is_final(api):
    c, calls = api
    fid = c.post("/fixes", json=FIX, headers=KEY).json()["id"]
    assert c.post(f"/fixes/{fid}/reject", headers=APPROVER).json()["status"] == "rejected"
    assert c.post(f"/fixes/{fid}/approve", headers=APPROVER).status_code == 409 and calls == []
    assert c.get("/fixes", headers=KEY).json() == {"pending": []}


def test_proposal_validation_and_repo_allowlist(api, monkeypatch):
    c, _ = api
    assert c.post("/fixes", json=FIX).status_code == 401
    assert c.post("/fixes", json={**FIX, "repo": "evil/x"}, headers=KEY).status_code == 403
    assert c.post("/fixes", json={**FIX, "path": ".github/workflows/ci.yml"}, headers=KEY).status_code == 422
    assert c.post("/fixes", json={**FIX, "title": " "}, headers=KEY).status_code == 422
    assert c.post("/fixes", json={**FIX, "content": 5}, headers=KEY).status_code == 422
    fid = c.post("/fixes", json=FIX, headers=KEY).json()["id"]
    monkeypatch.setenv("AGENT_PR_REPOS", "")  # allowlist shrank after the proposal: approval is refused too
    assert c.post(f"/fixes/{fid}/approve", headers=APPROVER).status_code == 403
    assert c.post("/fixes", json=FIX, headers=KEY).status_code == 403


def test_github_failure_is_recorded_and_not_retried_silently(api, monkeypatch):
    c, calls = api
    monkeypatch.setattr("agent.tools.github_pr.open_fix_pr",
                        lambda *a, **k: open_fix_pr(*a, token="t", client=gh(calls, fail_at="POST /pulls"), **k))
    fid = c.post("/fixes", json=FIX, headers=KEY).json()["id"]
    r = c.post(f"/fixes/{fid}/approve", headers=APPROVER)
    assert r.status_code == 502 and "pull request creation failed" in r.json()["detail"]
    got = c.get(f"/fixes/{fid}", headers=KEY).json()
    assert got["status"] == "failed" and "HTTP 422" in got["error"]
    assert c.post(f"/fixes/{fid}/approve", headers=APPROVER).status_code == 409


def test_feature_is_off_until_configured_and_in_product_mode(api, monkeypatch):
    c, _ = api
    monkeypatch.delenv("AGENT_APPROVALS_DB")
    assert c.get("/fixes", headers=KEY).status_code == 503
    monkeypatch.setenv("PRODUCT_DB", "x")
    assert c.get("/fixes", headers=KEY).status_code in (401, 404)  # tenant mode: not a multi-tenant feature
