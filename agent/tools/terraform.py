import re

_ACTION = re.compile(r"perform:?\s+([a-z0-9-]+:[A-Za-z0-9*]+)")
_ARN = re.compile(r"(arn:aws[a-z-]*:[^\s,]+)")
_ERROR_LINE = re.compile(r"^\s*(?:│\s*)?Error:\s*(.+)$", re.M)

# Ordered: first match wins. (kind, summary, lowercase substrings that identify it)
_KINDS = (
    ("iam_denied", "IAM permission missing for Terraform role", ("accessdenied", "not authorized to perform")),
    ("state_lock", "Terraform state is locked by another run", ("error acquiring the state lock",)),
    ("credentials", "AWS credentials missing or expired", ("no valid credential sources", "expiredtoken")),
)


def _line_with(log: str, needles) -> str | None:
    for line in log.splitlines():
        if any(n in line.lower() for n in needles):
            return line.strip()
    return None


def parse_error(log: str) -> dict | None:
    """Classify the first recognisable Terraform/CI error. Read-only text parsing.
    Returns {"kind", "summary", "action", "resource", "message", "line"} or None.
    `line` is the verbatim log line that identified the error (usable as evidence)."""
    if not log:
        return None

    action = (_ACTION.search(log) or [None, None])[1]
    resource = (_ARN.search(log) or [None, None])[1]
    err = _ERROR_LINE.search(log)
    message = err.group(1).strip() if err else None

    lowered = log.lower()
    for kind, summary, needles in _KINDS:
        if any(n in lowered for n in needles):
            line = _line_with(log, needles) or (err.group(0).strip() if err else None)
            break
    else:
        if not message:
            return None
        kind, summary, line = "other", message, err.group(0).strip()

    return {"kind": kind, "summary": summary, "action": action, "resource": resource,
            "message": message, "line": line}
