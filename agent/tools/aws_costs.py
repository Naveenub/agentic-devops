import boto3
from datetime import date, timedelta

def get_cost_breakdown(days=7):
    ce = boto3.client("ce")

    end = date.today()
    start = end - timedelta(days=days)

    response = ce.get_cost_and_usage(
        TimePeriod={
            "Start": start.strftime("%Y-%m-%d"),
            "End": end.strftime("%Y-%m-%d")
        },
        Granularity="DAILY",
        Metrics=["UnblendedCost"],
        GroupBy=[
            {"Type": "DIMENSION", "Key": "SERVICE"}
        ]
    )

    return response["ResultsByTime"]
