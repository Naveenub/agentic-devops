def get_pod_logs(pod_name: str) -> str:
    # Safe read-only mock for demo
    return (
        "OOMKilled: Container exceeded memory limit\n"
        "Back-off restarting failed container"
    )
