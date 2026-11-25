"""Phase 0 ship gates in one place: `python -m product.ship_gates` (make ship-gates). Exit 1 until every gate passes.

  real incidents  >= 20 real eval cases that pass the leakage check, and the MIN_REAL ratchet is >= 20
  pricing         python -m product.unit_economics check is clean
  legal           python -m product.legal check is clean (entity fields filled, counsel signed off)
  mail test       a real test email arrived (make mail-test sends it; once you have seen it in the inbox, record it, below)
  stripe e2e      the Stripe test-mode run passed (make stripe-e2e records it on success)

The first three are computed from the repo. The last two are real-world runs this code cannot repeat, so they are
read from product/ship_evidence.json (override with PRODUCT_SHIP_EVIDENCE): `make stripe-e2e` writes its entry itself, only
when every step passed; for mail you record it by hand after seeing the message arrive (SMTP accepting it is not delivery). Evidence older than PRODUCT_SHIP_EVIDENCE_MAX_DAYS (default 30) no longer counts: re-run it. The file is
self-attested; it stops a forgotten step from looking done, it does not prove the run happened.

  python -m product.ship_gates                      check all gates
  python -m product.ship_gates record mail_test --to addr     (after the test mail has arrived)
  python -m product.ship_gates record stripe_e2e              (called by stripe_e2e on success)"""
import contextlib
import io
import json
import os
import sys
import tempfile
import time
from datetime import date, datetime, timezone
from pathlib import Path

REAL_TARGET = 20
EVIDENCE_KINDS = ("mail_test", "stripe_e2e")


def evidence_path() -> Path:
    return Path(os.getenv("PRODUCT_SHIP_EVIDENCE") or Path(__file__).parent / "ship_evidence.json")


def load_evidence() -> dict:
    try:
        data = json.loads(evidence_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def record(kind: str, **details) -> None:
    if kind not in EVIDENCE_KINDS:
        raise ValueError(f"unknown evidence kind {kind!r}")
    data = load_evidence()
    data[kind] = {"date": date.today().isoformat(), **{k: v for k, v in details.items() if v}}
    path = evidence_path()
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".ship_evidence-")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, sort_keys=True)
        f.write("\n")
    os.replace(tmp, path)


def _max_age_days() -> int:
    try:
        return max(1, int(os.getenv("PRODUCT_SHIP_EVIDENCE_MAX_DAYS", "30")))
    except ValueError:
        return 30


def real_incidents() -> list:
    from agent import eval as agent_eval

    out = []
    try:
        cases = agent_eval.load_cases()
    except (ValueError, OSError) as e:
        return [f"real incidents: eval data does not load ({e})"]
    real = [c for c in cases if c["tier"] == "real"]
    clean = [c for c in real if not agent_eval.leakage(c)]
    if len(clean) < REAL_TARGET:
        out.append(f"real incidents: {len(clean)} usable real cases, need {REAL_TARGET}"
                   + (f" ({len(real) - len(clean)} rejected by the leakage check)" if len(real) != len(clean) else ""))
    if agent_eval.default_min_real() < REAL_TARGET:
        out.append(f"real incidents: data/eval/real/MIN_REAL is {agent_eval.default_min_real()}, "
                   f"raise it to {REAL_TARGET} so the set cannot shrink")
    return out


def _gap_lines(fn, ok_prefix: str, label: str) -> list:
    """Run a gate's own main() and return its gap lines (empty when it exits 0)."""
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = fn([])
    if rc == 0:
        return []
    lines = [ln.strip() for ln in buf.getvalue().splitlines() if ln.strip()]
    gaps = [f"{label}: {ln}" for ln in lines if not ln.startswith(ok_prefix) and not ln.endswith("NOT READY")]
    return gaps or [f"{label}: NOT READY"]


def pricing() -> list:
    from product import unit_economics

    return _gap_lines(unit_economics.main, "pricing:", "pricing")


def legal() -> list:
    from product import legal as legal_mod

    return _gap_lines(legal_mod.main, "legal:", "legal")


def evidence(kind: str, how: str) -> list:
    e = load_evidence().get(kind)
    if not isinstance(e, dict) or not e.get("date"):
        return [f"{kind}: no recorded run ({how})"]
    try:
        age = (datetime.now(timezone.utc).date() - date.fromisoformat(str(e["date"]))).days
    except ValueError:
        return [f"{kind}: unreadable date in {evidence_path().name} ({how})"]
    if age > _max_age_days():
        return [f"{kind}: last run {e['date']} is {age} days old, limit {_max_age_days()} ({how})"]
    if age < 0:
        return [f"{kind}: recorded date {e['date']} is in the future"]
    return []


def gates() -> dict:
    return {
        "real incidents": real_incidents(),
        "pricing": pricing(),
        "legal": legal(),
        "mail test": evidence("mail_test", "run: make mail-test TO=you@example.com"),
        "stripe e2e": evidence("stripe_e2e", "run: make stripe-e2e with STRIPE_SECRET_KEY=sk_test_..."),
    }


def main(argv: list) -> int:
    if argv[:1] == ["record"]:
        if len(argv) < 2 or argv[1] not in EVIDENCE_KINDS:
            print(f"usage: python -m product.ship_gates record [{'|'.join(EVIDENCE_KINDS)}] [--to addr]")
            return 2
        to = argv[argv.index("--to") + 1] if "--to" in argv and argv.index("--to") + 1 < len(argv) else None
        record(argv[1], to=to)
        print(f"recorded {argv[1]} in {evidence_path()}")
        return 0
    if argv:
        print("usage: python -m product.ship_gates [record ...]")
        return 2
    open_items = 0
    for name, problems in gates().items():
        print(f"[{'ok' if not problems else 'OPEN'}] {name}")
        for p in problems:
            print(f"      {p}")
        open_items += bool(problems)
    print("ship gates: ALL CLEAR" if not open_items else f"ship gates: {open_items} of 5 still open")
    return 1 if open_items else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
