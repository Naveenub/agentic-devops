import pytest

from agent import approvals
from agent.orchestrator import PIPELINE, run_agent

OOM = [{"source": "k", "content": "exit code 137"}]
FIX = {"repo": "o/r", "path": "infra/iam.tf", "content": "x\n", "title": "Grant iam:PassRole", "summary": "why"}


@pytest.fixture
def db(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_APPROVALS_DB", str(tmp_path / "a.db"))
    monkeypatch.setenv("AGENT_PR_REPOS", "o/r")


def test_pipeline_has_the_three_roles_in_order():
    names = [n for n, _ in PIPELINE]
    assert names[0] == "observer" and names[1] == "diagnoser" and names[-1] == "executor"


def test_without_a_fix_the_executor_does_nothing():
    assert "fix_proposal" not in run_agent({"question": "q", "observations": OOM})


def test_fix_is_queued_pending_and_nothing_is_opened(db, monkeypatch):
    def boom(*a, **k):
        raise AssertionError("executor stage must never open a PR")
    monkeypatch.setattr("agent.tools.github_pr.open_fix_pr", boom)
    r = run_agent({"question": "q", "observations": OOM, "fix": FIX})
    assert r["fix_proposal"]["status"] == "pending"
    assert approvals.get(r["fix_proposal"]["id"])["status"] == "pending"


def test_fix_outside_allowlist_is_refused(db, monkeypatch):
    monkeypatch.setenv("AGENT_PR_REPOS", "other/repo")
    r = run_agent({"question": "q", "observations": OOM, "fix": FIX})
    assert r["fix_proposal"]["status"] == "rejected_input" and "AGENT_PR_REPOS" in r["fix_proposal"]["error"]


def test_fix_without_approvals_db_is_unavailable(monkeypatch):
    monkeypatch.delenv("AGENT_APPROVALS_DB", raising=False)
    assert run_agent({"question": "q", "observations": OOM, "fix": FIX})["fix_proposal"]["status"] == "unavailable"


def test_approval_opens_pr_exactly_once(db, monkeypatch):
    from agent.executor import ExecutorError, execute_approved
    monkeypatch.setattr("agent.tools.github_pr.open_fix_pr",
                        lambda *a, **k: {"url": "https://github.com/o/r/pull/1", "number": 1})
    fid = run_agent({"question": "q", "observations": OOM, "fix": FIX})["fix_proposal"]["id"]
    assert execute_approved(fid, "dana")["status"] == "opened"
    with pytest.raises(ExecutorError) as e:
        execute_approved(fid, "dana")
    assert e.value.status == 409
