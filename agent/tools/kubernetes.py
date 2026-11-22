"""Read-only Kubernetes access. Only `kubectl logs` is exposed; no shell, fixed argv."""
import re
import subprocess

_NAME = re.compile(r"^[a-z0-9]([-a-z0-9.]{0,251}[a-z0-9])?$")  # DNS-1123, blocks flag injection


class KubernetesToolError(RuntimeError):
    pass


def _check(kind: str, value) -> str:
    if not isinstance(value, str) or not _NAME.match(value):
        raise KubernetesToolError(f"invalid {kind}: {value!r}")
    return value


def get_pod_logs(namespace: str, pod: str, tail: int = 200, previous: bool = False,
                 timeout: int = 15) -> str:
    argv = ["kubectl", "logs", _check("pod", pod), "-n", _check("namespace", namespace),
            f"--tail={int(tail)}"]
    if previous:
        argv.append("--previous")
    try:
        proc = subprocess.run(argv, capture_output=True, text=True, timeout=timeout, check=False)
    except FileNotFoundError as e:
        raise KubernetesToolError("kubectl not found on PATH") from e
    except subprocess.TimeoutExpired as e:
        raise KubernetesToolError(f"kubectl logs timed out after {timeout}s") from e
    if proc.returncode != 0:
        raise KubernetesToolError(proc.stderr.strip() or f"kubectl exited {proc.returncode}")
    return proc.stdout
