"""Failure signatures, shared by diagnose / suggest / confidence."""
import re
from typing import Optional, Tuple

UNKNOWN = "unknown"
COST_ANOMALY = "cost_anomaly"  # set by diagnose from the z-score verdict, not by a regex

# Ordered: first match wins.
SIGNATURES = [
    ("oom_killed", re.compile(r"oomkill|exit ?code:? ?137|java heap space|out of memory", re.I)),
    ("iam_denied", re.compile(r"accessdenied|not authorized to perform", re.I)),
    ("credentials_expired", re.compile(r"expiredtoken|token .{0,40}expired|credentials? .{0,20}expired", re.I)),
    ("image_pull", re.compile(r"imagepullbackoff|errimagepull", re.I)),
]

DIAGNOSIS = {
    "oom_killed": "Container was OOMKilled: memory limit too low for the workload.",
    "iam_denied": "Operation failed with an IAM AccessDenied: the calling role lacks a required permission.",
    "credentials_expired": "Credentials/security token used by the pipeline have expired.",
    "image_pull": "Pod cannot pull its image (bad tag or missing registry permissions).",
    "state_lock": "Terraform state is locked by another run (or a stale lock).",
    UNKNOWN: "Insufficient evidence: no recognised failure signature in the observations.",
}

SUGGESTIONS = {
    "oom_killed": [
        "Confirm: kubectl describe pod <pod> ; kubectl logs <pod> --previous",
        "Raise the container memory request/limit and redeploy through the normal pipeline.",
        "If usage keeps growing after the change, check for a leak / JVM heap settings.",
    ],
    "iam_denied": [
        "Identify the denied action and resource from the error message.",
        "Add only that action to the CI role's least-privilege policy.",
        "Re-run terraform plan before apply.",
    ],
    "credentials_expired": [
        "Rotate or re-issue the credentials used by the pipeline.",
        "Prefer short-lived OIDC federation over static keys.",
    ],
    "image_pull": [
        "Verify the image and tag exist in the registry.",
        "Check registry permissions / imagePullSecrets for the namespace.",
    ],
    "state_lock": [
        "Read the Lock Info (ID, Who, Created) in the error to find the run holding the lock.",
        "Wait for that run to finish; if it is dead, a human decides whether to release the stale lock.",
    ],
    COST_ANOMALY: [
        "Break the spike down by usage type and linked account in Cost Explorer to find the driver.",
        "Check this service/region for recent deploys, scaled-up resources or runaway jobs.",
        "Confirm with the owning team before changing anything; add a budget alert if it persists.",
    ],
    UNKNOWN: [
        "Collect more evidence (pod events, previous container logs, CI step output) and re-run.",
        "Escalate to a human if the impact is ongoing.",
    ],
}

# Sources (basenames) that corroborate a category.
CORROBORATING = {
    "oom_killed": {"eks-oomkill.md", "2024-11-eks-oom.json"},
    "iam_denied": {"terraform-iam-deny.md", "2024-12-tf-apply-fail.json"},
    "credentials_expired": {"2025-01-ci-permission.json"},
    "image_pull": {"imagepullbackoff.md"},
    "state_lock": {"terraform-state-lock.md"},
}

# agent.tools.terraform.parse_error kind -> category. Used when no regex signature matched,
# and to enrich (action/resource) when it agrees with the signature category.
TERRAFORM_CATEGORY = {
    "iam_denied": "iam_denied",
    "credentials": "credentials_expired",
    "state_lock": "state_lock",
}


def classify(text: str) -> Tuple[str, Optional[str]]:
    """Return (category, first matching line) or (UNKNOWN, None)."""
    for category, pattern in SIGNATURES:
        for line in text.splitlines():
            if pattern.search(line):
                return category, line.strip()
    return UNKNOWN, None
