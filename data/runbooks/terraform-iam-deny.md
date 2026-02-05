# Runbook: Terraform IAM AccessDenied

## Symptoms
- Terraform apply fails
- Error: AccessDeniedException

## Root Cause
IAM role used by CI pipeline lacks required permissions.

## Resolution
- Identify missing IAM actions from error
- Update least-privilege policy
- Re-run terraform plan before apply
