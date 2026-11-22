# Incident Report (synthetic dataset)

Dataset: `data/synthetic/incidents.json` - synthetic, not real telemetry. Mock observations are excluded from diagnosis and confidence.

- Cases: 8 | Passed: 8 | Failed: 0
- Category accuracy: 100.0%
- Mean confidence (known categories): 0.8

| ID | Expected | Got | Confidence | Cited | Mock obs | Result |
|---|---|---|---|---|---|---|
| SYN-001 | oom_killed | oom_killed | 0.80 | yes | no | PASS |
| SYN-002 | oom_killed | oom_killed | 0.80 | yes | no | PASS |
| SYN-003 | iam_denied | iam_denied | 0.80 | yes | no | PASS |
| SYN-004 | credentials_expired | credentials_expired | 0.80 | yes | no | PASS |
| SYN-005 | image_pull | image_pull | 0.80 | yes | no | PASS |
| SYN-006 | unknown | unknown | 0.00 | no | no | PASS |
| SYN-007 | unknown | unknown | 0.00 | no | yes | PASS |
| SYN-008 | iam_denied | iam_denied | 0.80 | yes | no | PASS |

## Evidence

**SYN-001**
- kubectl logs: Reason: OOMKilled
- retrieved: data/runbooks/eks-oomkill.md
- retrieved: data/incidents/2024-11-eks-oom.json

**SYN-002**
- kubectl logs: Exception in thread "main" java.lang.OutOfMemoryError: Java heap space
- retrieved: data/runbooks/eks-oomkill.md
- retrieved: data/incidents/2024-11-eks-oom.json

**SYN-003**
- ci log: Error: AccessDeniedException: User is not authorized to perform iam:PassRole
- retrieved: data/runbooks/terraform-iam-deny.md
- retrieved: data/incidents/2024-12-tf-apply-fail.json

**SYN-004**
- ci log: An error occurred (ExpiredToken) when calling the AssumeRole operation:
- retrieved: data/incidents/2025-01-ci-permission.json

**SYN-005**
- kubectl describe: Warning  Failed  kubelet  Error: ErrImagePull
- retrieved: data/runbooks/imagepullbackoff.md

**SYN-006**
- (none)

**SYN-007**
- (none)

**SYN-008**
- ci log: Error: AccessDeniedException: User is not authorized to perform s3:PutObject
- retrieved: data/runbooks/terraform-iam-deny.md
