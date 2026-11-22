"""Turn a raw incident log into a sanitized case stub for data/eval/real/.

  python -m agent.real_case --id REAL-001 --query "payments-api pods restarting" \\
      --source "kubectl logs" --log raw.log --category oom_killed --out data/eval/real/a.json

Reads the log from --log (or stdin), redacts obvious identifiers, and appends a case to --out
(a JSON list, created if missing). It never labels from agent output: --category comes from you, from the
postmortem. Redaction is best-effort: read the result before committing it.
"""
import argparse
import json
import re
import sys
from pathlib import Path
from typing import Dict, List, Tuple

from agent.signatures import SIGNATURES, UNKNOWN
from agent.state import LLM_CATEGORY

VALID_CATEGORIES = tuple(n for n, _ in SIGNATURES) + ("state_lock", "terraform_drift", LLM_CATEGORY, UNKNOWN)

# Ordered: specific secrets first so the generic patterns do not half-redact them.
_RULES: List[Tuple[str, re.Pattern, str]] = [
    ("private_key", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----", re.S), "<REDACTED_KEY>"),
    ("aws_key", re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"), "AKIAXXXXXXXXXXXXXXXX"),
    ("github_token", re.compile(r"\b(?:ghp|gho|ghs|ghu|github_pat)_[A-Za-z0-9_]{20,}\b"), "<REDACTED_TOKEN>"),
    ("slack_token", re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}\b"), "<REDACTED_TOKEN>"),
    ("bearer", re.compile(r"(?i)\b(bearer|token|authorization:?|api[_-]?key|secret|password)([\s:=\"']+)(?!<)[A-Za-z0-9._~+/=-]{16,}"), r"\1\2<REDACTED>"),
    ("email", re.compile(r"\b[\w.+-]+@[\w-]+(?:\.[\w-]+)+\b"), "user@example.com"),
    ("account_id", re.compile(r"(?<!\d)\d{12}(?!\d)"), "000000000000"),
    ("ipv4", re.compile(r"\b(?:(?:25[0-5]|2[0-4]\d|1?\d?\d)\.){3}(?:25[0-5]|2[0-4]\d|1?\d?\d)\b"), "10.0.0.1"),
    ("ec2_host", re.compile(r"\b(?:ip-\d{1,3}-\d{1,3}-\d{1,3}-\d{1,3}|i-[0-9a-f]{8,17})(?:\.[\w.-]+)?\b"), "node-example"),
]


def sanitize(text: str) -> Tuple[str, Dict[str, int]]:
    """Return (redacted text, {rule: count}). Idempotent: running it twice changes nothing more."""
    counts: Dict[str, int] = {}
    for name, pat, repl in _RULES:
        text, n = pat.subn(repl, text)
        if n:
            counts[name] = n
    return text, counts


def build_case(case_id: str, query: str, source: str, log: str, category: str,
               keywords: List[str], sources: List[str]) -> Tuple[dict, Dict[str, int]]:
    if category not in VALID_CATEGORIES:
        raise ValueError(f"category must be one of {VALID_CATEGORIES}, got {category!r}")
    if not keywords:
        raise ValueError("--keyword is required (use the postmortem's wording for the root cause)")
    signature = category not in (LLM_CATEGORY, UNKNOWN)
    if signature and not sources:
        raise ValueError("--expected-source is required for signature categories")
    content, counts = sanitize(log.strip("\n"))
    case = {"id": case_id, "query": query, "observations": [{"source": source, "content": content}],
            "expected_category": category, "root_cause_keywords": keywords}
    if signature:
        case["expected_sources"] = sources
    return case, counts


def main(argv=()) -> int:
    ap = argparse.ArgumentParser(prog="python -m agent.real_case", description=__doc__.split("\n")[0])
    ap.add_argument("--id", required=True)
    ap.add_argument("--query", required=True, help="what the on-call actually asked")
    ap.add_argument("--source", default="kubectl logs")
    ap.add_argument("--log", help="raw log file (default: stdin)")
    ap.add_argument("--category", required=True, help=" | ".join(VALID_CATEGORIES))
    ap.add_argument("--keyword", action="append", default=[], help="root-cause word from the postmortem (repeatable)")
    ap.add_argument("--expected-source", action="append", default=[], help="runbook/incident path (signature categories only)")
    ap.add_argument("--out", required=True, help="data/eval/real/<name>.json (JSON list, appended to)")
    args = ap.parse_args(list(argv))

    raw = Path(args.log).read_text(encoding="utf-8") if args.log else sys.stdin.read()
    try:
        case, counts = build_case(args.id, args.query, args.source, raw, args.category, args.keyword, args.expected_source)
    except ValueError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2

    out = Path(args.out)
    existing = json.loads(out.read_text(encoding="utf-8")) if out.exists() else []
    if any(c.get("id") == args.id for c in existing):
        print(f"error: {args.id} already in {out}", file=sys.stderr)
        return 2
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(existing + [case], indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"wrote {args.id} to {out}; redacted: {counts or 'nothing (check the log by eye)'}")
    print("next: read the case, then `python -m agent.eval` (the leakage check runs on it)")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
