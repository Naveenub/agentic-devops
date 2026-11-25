"""Phase 0 ship gates: the aggregate must stay closed until each real-world step has been done."""
import json
from datetime import date, timedelta

import pytest

from product import ship_gates


@pytest.fixture(autouse=True)
def evidence_file(tmp_path, monkeypatch):
    monkeypatch.setenv("PRODUCT_SHIP_EVIDENCE", str(tmp_path / "ev.json"))
    return tmp_path / "ev.json"


def test_no_evidence_means_open():
    assert "no recorded run" in ship_gates.evidence("mail_test", "how")[0]
    assert "no recorded run" in ship_gates.evidence("stripe_e2e", "how")[0]


def test_record_then_clear_and_cli(evidence_file):
    assert ship_gates.main(["record", "mail_test", "--to", "me@example.com"]) == 0
    assert json.loads(evidence_file.read_text())["mail_test"]["to"] == "me@example.com"
    assert ship_gates.evidence("mail_test", "how") == []


def test_stale_or_future_or_garbled_evidence_does_not_count(evidence_file):
    for when in ((date.today() - timedelta(days=31)).isoformat(), (date.today() + timedelta(days=2)).isoformat(), "yesterday"):
        evidence_file.write_text(json.dumps({"stripe_e2e": {"date": when}}))
        assert ship_gates.evidence("stripe_e2e", "how"), when


def test_age_limit_is_configurable(evidence_file, monkeypatch):
    evidence_file.write_text(json.dumps({"stripe_e2e": {"date": (date.today() - timedelta(days=45)).isoformat()}}))
    monkeypatch.setenv("PRODUCT_SHIP_EVIDENCE_MAX_DAYS", "60")
    assert ship_gates.evidence("stripe_e2e", "how") == []


def test_unknown_kind_and_bad_usage_rejected():
    with pytest.raises(ValueError):
        ship_gates.record("nope")
    assert ship_gates.main(["record", "nope"]) == 2
    assert ship_gates.main(["bogus"]) == 2


def test_aggregate_is_closed_in_this_repo_state(capsys):
    # Entity fields, pricing inputs and real incidents are facts only the owner can supply; until they are, exit 1.
    assert ship_gates.main([]) == 1
    out = capsys.readouterr().out
    assert "[OPEN] real incidents" in out and "ship gates:" in out


def test_real_incident_gate_counts_only_leak_free_cases(monkeypatch):
    from agent import eval as agent_eval

    real = [{"id": f"R{i}", "tier": "real"} for i in range(20)]
    monkeypatch.setattr(agent_eval, "load_cases", lambda *a, **k: real)
    monkeypatch.setattr(agent_eval, "leakage", lambda c: [])
    monkeypatch.setattr(agent_eval, "default_min_real", lambda *a, **k: 20)
    assert ship_gates.real_incidents() == []
    monkeypatch.setattr(agent_eval, "leakage", lambda c: ["leak"] if c["id"] == "R0" else [])
    assert "19 usable" in ship_gates.real_incidents()[0]
    monkeypatch.setattr(agent_eval, "leakage", lambda c: [])
    monkeypatch.setattr(agent_eval, "default_min_real", lambda *a, **k: 5)
    assert "MIN_REAL" in ship_gates.real_incidents()[0]


def test_stripe_e2e_records_only_on_a_clean_run(monkeypatch, evidence_file):
    from product import stripe_e2e

    monkeypatch.setenv("STRIPE_SECRET_KEY", "sk_test_x")
    monkeypatch.setattr(stripe_e2e, "preflight", lambda: None)
    monkeypatch.setattr(stripe_e2e, "run", lambda plan, keep: None)
    stripe_e2e.results.clear()
    stripe_e2e.results.append(("step", True, ""))
    assert stripe_e2e.main([]) == 0
    assert "stripe_e2e" in json.loads(evidence_file.read_text())
    evidence_file.unlink()
    stripe_e2e.results.clear()
    stripe_e2e.results.append(("step", False, "boom"))
    assert stripe_e2e.main([]) == 1
    assert not evidence_file.exists()
