# Incident Runbook: EKS Pod OOMKill

## Symptoms
- Pod enters CrashLoopBackOff
- Container terminated with exit code 137
- Kubernetes events show `OOMKilled`

## Root Cause
The container memory limit was too low for peak traffic load, causing the kernel to kill the process.

## Verification
```bash
kubectl describe pod <pod-name>
kubectl logs <pod-name> --previous
