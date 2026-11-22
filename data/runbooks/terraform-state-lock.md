# Runbook: Terraform State Lock

## Symptoms
- Terraform plan or apply fails in CI
- Error: Error acquiring the state lock

## Root Cause
Another Terraform run holds the state lock, or a crashed run left a stale lock.

## Resolution
- Read the Lock Info in the error (ID, Who, Created) to find the run holding the lock
- Wait for that run to finish
- If that run is dead, a human releases the stale lock; never release a lock held by an active run
- Re-run terraform plan
