"""AWS cost anomaly detection: z-score over daily Cost Explorer spend. Read-only
(`ce:GetCostAndUsage`). boto3 is imported lazily; no data -> no verdict (never a guess)."""
import statistics
from datetime import date, timedelta
from typing import List, Optional

MIN_POINTS = 7


class CostToolError(RuntimeError):
    pass


def fetch_daily_costs(service: str, region: str, days: int = 30, client=None) -> List[float]:
    """Daily UnblendedCost for one service/region, oldest first. `client` is injectable."""
    if client is None:
        try:
            import boto3
        except ImportError as e:
            raise CostToolError("boto3 is not installed") from e
        client = boto3.client("ce", region_name="us-east-1")  # Cost Explorer endpoint

    end = date.today()
    try:
        resp = client.get_cost_and_usage(
            TimePeriod={"Start": (end - timedelta(days=days)).isoformat(), "End": end.isoformat()},
            Granularity="DAILY",
            Metrics=["UnblendedCost"],
            Filter={"And": [
                {"Dimensions": {"Key": "SERVICE", "Values": [service]}},
                {"Dimensions": {"Key": "REGION", "Values": [region]}},
            ]},
        )
    except Exception as e:  # botocore errors: credentials, permissions, throttling
        raise CostToolError(f"Cost Explorer call failed: {e}") from e
    return [float(r["Total"]["UnblendedCost"]["Amount"]) for r in resp["ResultsByTime"]]


def zscore_latest(values: List[float]) -> Optional[float]:
    """z-score of the last value against all previous ones; None if undefined."""
    history, latest = values[:-1], values[-1]
    if len(history) < MIN_POINTS - 1:
        return None
    stdev = statistics.pstdev(history)
    mean = statistics.fmean(history)
    if stdev == 0:
        return 0.0 if latest == mean else float("inf")
    return (latest - mean) / stdev


def analyze_cost_anomaly(service: str, region: str, daily_costs: Optional[List[float]] = None,
                         threshold: float = 2.0, client=None) -> dict:
    """{"anomaly": bool|None, ...}. anomaly is None when there is not enough data or the fetch failed."""
    try:
        values = daily_costs if daily_costs is not None else fetch_daily_costs(service, region, client=client)
    except CostToolError as e:
        return {"anomaly": None, "error": str(e), "message": f"No verdict: {e}"}

    z = zscore_latest(values) if values else None
    if z is None:
        return {"anomaly": None, "n_points": len(values),
                "message": f"No verdict: need at least {MIN_POINTS} daily data points."}

    return {
        "anomaly": z > threshold,
        "z_score": round(z, 2) if z != float("inf") else z,
        "latest": values[-1],
        "mean": round(statistics.fmean(values[:-1]), 2),
        "n_points": len(values),
        "threshold": threshold,
        "message": f"{service} in {region}: latest daily cost z={z:.2f} "
                   f"({'above' if z > threshold else 'within'} threshold {threshold}).",
    }
