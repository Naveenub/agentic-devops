"""Running more than one process: the billing schedule, atomic limits, and the PostgreSQL backend.

Every test here runs on SQLite; set TEST_POSTGRES_URL (e.g. postgresql://user@host/db, an empty throwaway database)
to run the same tests against PostgreSQL too. tests/test_product.py honours the same variable."""
import asyncio
import contextlib
import json
import os
import threading
import time

import pytest
from fastapi.testclient import TestClient

from api.main import app
from product import billing, scheduler, store


@pytest.fixture(params=["sqlite", "postgres"])
def db(request, tmp_path, monkeypatch):
    if request.param == "postgres":
        url = os.getenv("TEST_POSTGRES_URL")
        if not url:
            pytest.skip("set TEST_POSTGRES_URL to run against PostgreSQL")
        monkeypatch.setenv("PRODUCT_DB", url)
        with store.conn() as c:
            c.execute("TRUNCATE tenants, stripe_events, api_keys, usage, rate_hits, slack_links, signup_hits, "
                      "recover_hits, recovery_tokens, job_runs RESTART IDENTITY CASCADE")
    else:
        monkeypatch.setenv("PRODUCT_DB", str(tmp_path / "p.db"))
    return request.param


def _race(n, fn):
    """Run fn() on n threads released together; returns their results."""
    out, gate = [], threading.Barrier(n)

    def run():
        gate.wait()
        out.append(fn())
    ts = [threading.Thread(target=run) for _ in range(n)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    return out


def test_backend_is_picked_from_the_url():
    assert store.is_postgres("postgresql://u@h/d") and store.is_postgres("postgres://u@h/d")
    assert not store.is_postgres("/data/product.db")


def test_claim_run_allows_one_run_per_interval(db, monkeypatch):
    assert store.claim_run("job", 60) is True
    assert store.claim_run("job", 60) is False
    assert store.claim_run("other", 60) is True  # jobs are independent
    now = time.time()
    monkeypatch.setattr(store.time, "time", lambda: now + 61)
    assert store.claim_run("job", 60) is True


def test_only_one_of_many_racing_workers_wins_the_claim(db):
    assert _race(12, lambda: store.claim_run("billing-report", 3600)).count(True) == 1


def test_quota_cannot_be_overshot_by_concurrent_requests(db):
    tid = store.create_tenant("acme")["id"]

    def one():
        try:
            store.reserve(tid, "analyze", 1, lambda used: "over quota" if used.get("analyze", 0) >= 5 else None)
            return True
        except store.QuotaRefused:
            return False
    assert _race(20, one).count(True) == 5
    assert store.usage_since(tid, 0) == {"analyze": 5}


def test_rate_limit_holds_across_concurrent_requests(db):
    tid = store.create_tenant("acme")["id"]
    assert _race(20, lambda: store.rate_hit(tid, 7)).count(True) == 7


def test_duplicate_email_and_slack_relink_work_on_both_backends(db):
    a = store.create_tenant("a", email="a@example.com")["id"]
    with pytest.raises(store.EmailTaken):
        store.create_tenant("b", email="a@example.com")
    b = store.create_tenant("b")["id"]
    store.link_slack(a, "T1")
    store.link_slack(b, "T1")  # re-linking moves the workspace
    assert store.tenant_for_slack("T1")["id"] == b


def test_run_due_reports_only_when_the_claim_is_won(db):
    calls = []
    assert scheduler.run_due(3600, lambda: calls.append(1) or {"reported": 0}) == {"reported": 0}
    assert scheduler.run_due(3600, lambda: calls.append(1)) is None
    assert calls == [1]


def test_scheduler_loop_survives_a_failing_run(db):
    calls = []

    def report():
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("stripe down")
        return {"reported": 0}

    async def go():
        t = asyncio.create_task(scheduler.billing_forever(0.01, report))
        await asyncio.sleep(0.4)
        t.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await t
    asyncio.run(go())
    assert len(calls) >= 2


@pytest.mark.parametrize("raw,want", [(None, 0), ("", 0), ("abc", 0), ("-5", 0), ("0", 0), ("3600", 3600.0)])
def test_interval_parsing(monkeypatch, raw, want):
    monkeypatch.delenv("PRODUCT_BILLING_INTERVAL", raising=False)
    if raw is not None:
        monkeypatch.setenv("PRODUCT_BILLING_INTERVAL", raw)
    assert scheduler.interval() == want


def _claimed() -> bool:
    deadline = time.time() + 5
    while time.time() < deadline:
        with store.conn() as c:
            if c.execute("SELECT 1 FROM job_runs WHERE name=?", (scheduler.JOB,)).fetchone():
                return True
        time.sleep(0.05)
    return False


def test_app_runs_the_billing_schedule_only_when_asked(db, monkeypatch):
    monkeypatch.delenv("STRIPE_SECRET_KEY", raising=False)
    monkeypatch.delenv("COST_WATCH", raising=False)
    monkeypatch.delenv("PRODUCT_BILLING_INTERVAL", raising=False)
    with TestClient(app):  # entering the client runs the app's startup
        time.sleep(0.3)
    with store.conn() as c:
        assert not c.execute("SELECT 1 FROM job_runs").fetchone()
    monkeypatch.setenv("PRODUCT_BILLING_INTERVAL", "3600")
    with TestClient(app):
        assert _claimed()


def test_cli_exit_status_reflects_failed_batches(monkeypatch, capsys):
    monkeypatch.setattr(billing, "report_usage", lambda: {"reported": 2, "failed": 0})
    assert billing.main() == 0
    assert json.loads(capsys.readouterr().out) == {"reported": 2, "failed": 0}
    monkeypatch.setattr(billing, "report_usage", lambda: {"reported": 1, "failed": 1})
    assert billing.main() == 1


def test_report_holds_no_transaction_while_calling_stripe(db, monkeypatch):
    """A slow Stripe call must not keep the database locked: another writer gets in mid-report."""
    monkeypatch.setenv("STRIPE_SECRET_KEY", "sk")
    monkeypatch.setenv("STRIPE_METER_ANALYZE", "agent_analyze_runs")
    t = store.create_tenant("acme")
    store.update_tenant(t["id"], stripe_customer_id="cus_1")
    store.record(t["id"], "analyze", 3)
    seen = []

    class R:
        status_code = 200

    def post(data):
        store.record(t["id"], "analyze", 1)  # would block (SQLite) / deadlock-wait (Postgres) if a tx were open
        seen.append(data["payload[value]"])
        return R()
    assert billing.report_usage(post=post) == {"reported": 1, "failed": 0}
    assert seen == ["3"]
    assert billing.report_usage(post=lambda d: R())["reported"] == 1  # the row added mid-report goes out next time
