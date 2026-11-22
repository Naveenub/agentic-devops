import re

_ACTION = re.compile(r"perform:?\s+([a-z0-9-]+:[A-Za-z0-9*]+)")
_ARN = re.compile(r"(arn:aws[a-z-]*:[^\s,]+)")
_ERROR_LINE = re.compile(r"^\s*(?:│\s*)?Error:\s*(.+)$", re.M)


def parse_error(log: str) -> dict | None:
    """Classify the first recognisable Terraform/CI error. Read-only text parsing.
    Returns {"kind", "summary", "action", "resource", "message"} or None."""
    if not log:
        return None

    action = (_ACTION.search(log) or [None, None])[1]
    resource = (_ARN.search(log) or [None, None])[1]
    err = _ERROR_LINE.search(log)
    message = err.group(1).strip() if err else None

    lowered = log.lower()
    if "accessdenied" in lowered or "not authorized to perform" in lowered:
        kind, summary = "iam_denied", "IAM permission missing for Terraform role"
    elif "error acquiring the state lock" in lowered:
        kind, summary = "state_lock", "Terraform state is locked by another run"
    elif "no valid credential sources" in lowered or "expiredtoken" in lowered:
        kind, summary = "credentials", "AWS credentials missing or expired"
    elif message:
        kind, summary = "other", message
    else:
        return None

    return {"kind": kind, "summary": summary, "action": action, "resource": resource, "message": message}
