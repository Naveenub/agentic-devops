"""Repeatable incident report over the synthetic dataset. Deterministic: no timestamps,
offline retriever, mocks excluded from evidence.

  python -m agent.report            # write reports/incident_report.{md,json}
  python -m agent.report --check    # fail if committed reports differ from a fresh run
"""
import argparse
import json
import os
import sys
from pathlib import Path

os.environ["AGENT_RETRIEVER"] = "local"  # reproducible regardless of caller env
os.environ.pop("GITHUB_TOKEN", None)     # never hit the network: the github tool then fails fast

from agent.orchestrator import run_agent  # noqa: E402
from agent.signatures import UNKNOWN  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
DATASET = ROOT / "data" / "synthetic" / "incidents.json"
OUT_DIR = ROOT / "reports"


def run_case(case: dict) -> dict:
    state = {"question": case["question"]}
    for key in ("observations", "github", "kubernetes"):
        if key in case:
            state[key] = case[key]
    result = run_agent(state)

    expected = case["expected_category"]
    got = result["category"]
    conf = result["confidence"]
    cited = any(e.startswith("retrieved: ") for e in result["evidence"])
    has_mock = any(o.get("mock") for o in result["observations"])
    has_error = any(o.get("error") for o in result["observations"])

    checks = {"category": got == expected}
    if expected == UNKNOWN:
        checks["abstained"] = conf == 0.0
    else:
        checks["confidence"] = conf >= case.get("min_confidence", 0.6)
        checks["cited"] = cited
    if case.get("expect_mock_observation"):
        checks["mock_flagged"] = has_mock
    if case.get("expect_error_observation"):
        checks["error_flagged"] = has_error

    return {
        "id": case["id"],
        "expected": expected,
        "got": got,
        "confidence": conf,
        "cited": cited,
        "mock_observation": has_mock,
        "error_observation": has_error,
        "evidence": result["evidence"],
        "passed": all(checks.values()),
        "failed_checks": sorted(k for k, v in checks.items() if not v),
    }


def build_report(dataset_path: Path = DATASET) -> dict:
    data = json.loads(dataset_path.read_text(encoding="utf-8"))
    rows = [run_case(c) for c in data["cases"]]
    passed = sum(r["passed"] for r in rows)
    known = [r for r in rows if r["expected"] != UNKNOWN]
    return {
        "dataset": dataset_path.relative_to(ROOT).as_posix(),
        "synthetic": data["meta"]["synthetic"],
        "summary": {
            "cases": len(rows),
            "passed": passed,
            "failed": len(rows) - passed,
            "category_accuracy": round(sum(r["got"] == r["expected"] for r in rows) / len(rows), 3),
            "mean_confidence_known": round(sum(r["confidence"] for r in known) / max(len(known), 1), 3),
        },
        "cases": rows,
    }


def to_markdown(report: dict) -> str:
    s = report["summary"]
    lines = [
        "# Incident Report (synthetic dataset)",
        "",
        f"Dataset: `{report['dataset']}` - synthetic, not real telemetry. "
        "Mock and failed-tool observations are excluded from diagnosis and confidence.",
        "",
        f"- Cases: {s['cases']} | Passed: {s['passed']} | Failed: {s['failed']}",
        f"- Category accuracy: {s['category_accuracy']:.1%}",
        f"- Mean confidence (known categories): {s['mean_confidence_known']}",
        "",
        "| ID | Expected | Got | Confidence | Cited | Mock obs | Tool error | Result |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for r in report["cases"]:
        lines.append(
            f"| {r['id']} | {r['expected']} | {r['got']} | {r['confidence']:.2f} | "
            f"{'yes' if r['cited'] else 'no'} | {'yes' if r['mock_observation'] else 'no'} | "
            f"{'yes' if r['error_observation'] else 'no'} | "
            f"{'PASS' if r['passed'] else 'FAIL: ' + ', '.join(r['failed_checks'])} |"
        )
    lines += ["", "## Evidence", ""]
    for r in report["cases"]:
        lines.append(f"**{r['id']}**")
        lines += [f"- {e}" for e in r["evidence"]] or ["- (none)"]
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def render(report: dict) -> dict:
    return {
        "incident_report.md": to_markdown(report),
        "incident_report.json": json.dumps(report, indent=2, sort_keys=True) + "\n",
    }


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true", help="verify committed reports are current")
    ap.add_argument("--out-dir", type=Path, default=OUT_DIR)
    args = ap.parse_args(argv)

    report = build_report()
    files = render(report)
    status = 0

    if args.check:
        for name, content in files.items():
            path = args.out_dir / name
            if not path.exists() or path.read_text(encoding="utf-8") != content:
                print(f"STALE: {path} (run `make report`)", file=sys.stderr)
                status = 1
    else:
        args.out_dir.mkdir(parents=True, exist_ok=True)
        for name, content in files.items():
            (args.out_dir / name).write_text(content, encoding="utf-8")
            print(f"wrote {args.out_dir / name}")

    if report["summary"]["failed"]:
        print(f"{report['summary']['failed']} case(s) failed", file=sys.stderr)
        status = 1
    return status


if __name__ == "__main__":
    sys.exit(main())
