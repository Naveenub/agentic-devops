"""Cost anomaly checks shared by the API, the Slack command and the scheduler.

  COST_WATCH="EC2:us-east-1;S3:us-east-1"   # service:region pairs, ';'-separated
  python -m agent.cost_watch [--notify]     # one live pass; prints verdicts, alerts only with --notify
"""
import argparse
import asyncio
import json
import logging
import math
import os
import sys
import time
import urllib.request
from typing import Dict, List, Tuple

from agent.orchestrator import run_agent

log = logging.getLogger(__name__)

PD_URL = "https://events.pagerduty.com/v2/enqueue"

# Cost Explorer filters on the full SERVICE dimension value, not the short name.
SERVICE_ALIASES = {
    "ec2": "Amazon Elastic Compute Cloud - Compute",
    "s3": "Amazon Simple Storage Service",
    "rds": "Amazon Relational Database Service",
    "lambda": "AWS Lambda",
    "eks": "Amazon Elastic Container Service for Kubernetes",
}


def resolve_service(name: str) -> str:
    name = name.strip()
    return SERVICE_ALIASES.get(name.lower(), name)


def parse_watch(spec) -> List[Tuple[str, str]]:
    pairs = []
    for item in (spec or "").split(";"):
        item = item.strip()
        if not item:
            continue
        service, _, region = item.rpartition(":")
        if not service.strip() or not region.strip():
            raise ValueError(f"COST_WATCH entries must be service:region, got {item!r}")
        pairs.append((service.strip(), region.strip()))
    return pairs


def _page(service: str, region: str, anomaly: dict, suggestion: str) -> None:
    """PagerDuty Events API v2 trigger. dedup_key makes repeats update one incident."""
    body = {
        "routing_key": os.environ["PAGERDUTY_ROUTING_KEY"],
        "event_action": "trigger",
        "dedup_key": f"cost-anomaly:{service}:{region}",
        "payload": {
            "summary": f"Cost anomaly: {anomaly['message']}",
            "source": "agentic-devops",
            "severity": "warning",
            "custom_details": {"latest": anomaly["latest"], "mean": anomaly["mean"],
                               "z_score": anomaly["z_score"], "suggestion": suggestion},
        },
    }
    req = urllib.request.Request(PD_URL, json.dumps(body).encode(), {"Content-Type": "application/json"})
    urllib.request.urlopen(req, timeout=10).close()


def check_cost(service: str, region: str, threshold: float = 2.0, notify: bool = True) -> dict:
    """Run the agent on one service/region. On a spike and notify=True, alert Slack
    (SLACK_ALERT_CHANNEL) and page PagerDuty (PAGERDUTY_ROUTING_KEY) when configured."""
    service = resolve_service(service)
    result = run_agent({"question": f"Cost check: {service} in {region}",
                        "cost": {"service": service, "region": region, "threshold": threshold}})
    anomaly = dict(result["cost_anomaly"])
    if not math.isfinite(anomaly.get("z_score", 0.0)):  # flat history + spike; inf is not valid JSON
        anomaly["z_score"] = "inf"
    out = {"cost_anomaly": anomaly, "diagnosis": result["diagnosis"],
           "suggestions": result["suggestions"], "alerted": False, "paged": False}
    if not (notify and anomaly["anomaly"] is True):
        return out

    suggestion = result["suggestions"][0]
    channel = os.getenv("SLACK_ALERT_CHANNEL")
    if channel:
        try:
            from agent.tools.slack import send_message  # lazy: slack_sdk only needed here

            send_message(channel, f":rotating_light: *Cost anomaly* - {anomaly['message']}\n"
                                  f"*Latest:* {anomaly['latest']} vs mean {anomaly['mean']}\n"
                                  f"*Suggested:* {suggestion}")
            out["alerted"] = True
        except Exception as e:  # detection succeeded; don't lose it because Slack failed
            out["alert_error"] = str(e)
    if os.getenv("PAGERDUTY_ROUTING_KEY"):
        try:
            _page(service, region, anomaly, suggestion)
            out["paged"] = True
        except Exception as e:
            out["page_error"] = str(e)
    return out


def run_watch_once(watch, last_alert: Dict[tuple, float], cooldown: float, now: float = None) -> List[dict]:
    """One scheduler pass. A persisting spike re-alerts only after `cooldown`; a clean verdict resets it."""
    now = time.time() if now is None else now
    results = []
    for service, region in watch:
        key = (service, region)
        notify = now - last_alert.get(key, -math.inf) >= cooldown
        try:
            r = check_cost(service, region, notify=notify)
        except Exception:
            log.exception("cost check failed for %s/%s", service, region)
            continue
        if r["alerted"] or r["paged"]:
            last_alert[key] = now
        elif r["cost_anomaly"]["anomaly"] is False:
            last_alert.pop(key, None)
        results.append(r)
    return results


async def watch_forever(watch, interval: float, cooldown: float) -> None:
    interval = max(interval, 60.0)  # Cost Explorer bills per request; data only refreshes a few times a day
    last_alert: Dict[tuple, float] = {}
    while True:
        await asyncio.to_thread(run_watch_once, watch, last_alert, cooldown)
        await asyncio.sleep(interval)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="One live pass over COST_WATCH.")
    ap.add_argument("--notify", action="store_true", help="send Slack/PagerDuty alerts on a spike")
    args = ap.parse_args(argv)
    watch = parse_watch(os.getenv("COST_WATCH"))
    if not watch:
        print("COST_WATCH is empty", file=sys.stderr)
        return 2
    status = 0
    for service, region in watch:
        r = check_cost(service, region, notify=args.notify)
        print(json.dumps(r["cost_anomaly"], default=str))
        if r["cost_anomaly"]["anomaly"] is None:
            status = 1  # no verdict: credentials, permissions or too little data
    return status


if __name__ == "__main__":
    sys.exit(main())
