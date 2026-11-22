"""Provision Stripe from product/plans.py and detect drift from it.

  python -m product.stripe_sync            check only: prints drift, exit 1 if Stripe differs from plans.py
  python -m product.stripe_sync --apply    create/replace what is missing or different, print the env to set

Per paid plan this manages one flat monthly price (`agentic_<plan>_base`) and, for every metered event with an
overage rate, one graduated-tier price (`agentic_<plan>_<event>`): first tier = the plan's included quota at 0,
second tier = the overage rate. Stripe prices are immutable, so a changed price/quota creates a new price and moves
the lookup key to it; existing subscribers keep the old price until you migrate them. Meters are created once per
event (METERS). Safe to re-run: everything is found by lookup key / event name. GET /admin/billing/drift runs the check.
"""
import hashlib
import os
import sys
from decimal import Decimal

import httpx

from product.plans import METERS, _load
from product.subscriptions import API, StripeError


def _http():
    key = os.getenv("STRIPE_SECRET_KEY")
    if not key:
        raise StripeError("STRIPE_SECRET_KEY is not configured", 503)

    def call(method: str, path: str, data: dict | None = None, idem: str | None = None):
        kw = {"params": data} if method == "GET" else {"data": data}
        try:
            return httpx.request(method, f"{API}{path}", auth=(key, ""), timeout=20,
                                 headers={"Idempotency-Key": idem} if idem else {}, **kw)
        except httpx.HTTPError as e:
            raise StripeError(f"Stripe unreachable: {e}")
    return call


def _ok(r) -> dict:
    if r.status_code >= 300:
        raise StripeError(f"Stripe error {r.status_code}: {getattr(r, 'text', '')[:200]}")
    return r.json()


def desired(plans: dict | None = None) -> list[dict]:
    """Every Stripe price plans.py implies, base first per plan."""
    items = []
    for p in (plans or _load()).values():
        if not p.base_cents:  # free or not sold online: nothing to provision
            continue
        items.append({"key": f"agentic_{p.name}_base", "plan": p.name, "event": None, "amount": p.base_cents,
                      "currency": p.currency.lower()})
        for ev, (quota, rate) in p.billed_events().items():
            items.append({"key": f"agentic_{p.name}_{ev}", "plan": p.name, "event": ev, "quota": quota,
                          "rate": rate, "currency": p.currency.lower()})
    return items


def _tiers(item: dict) -> list[tuple]:
    first = [(item["quota"], Decimal(0))] if item["quota"] else []
    return first + [(None, item["rate"])]


def problems(item: dict, price: dict, meter_ids: dict) -> list[str]:
    """How an existing Stripe price differs from the catalog ([] = in sync)."""
    out, rec = [], price.get("recurring") or {}
    if (price.get("currency") or "").lower() != item["currency"]:
        out.append(f"currency {price.get('currency')} != {item['currency']}")
    if rec.get("interval") != "month" or int(rec.get("interval_count") or 1) != 1:
        out.append("interval is not monthly")
    if item["event"] is None:
        if rec.get("usage_type") != "licensed" or price.get("billing_scheme") == "tiered":
            out.append("not a flat licensed price")
        if price.get("unit_amount") != item["amount"]:
            out.append(f"amount {price.get('unit_amount')} != {item['amount']}")
        return out
    if rec.get("usage_type") != "metered" or rec.get("meter") != meter_ids.get(item["event"]):
        out.append("not metered on the expected meter")
    if price.get("billing_scheme") != "tiered" or price.get("tiers_mode") != "graduated":
        out.append("not graduated tiered")
    have = [(t.get("up_to"), Decimal(str(t.get("unit_amount_decimal") or t.get("unit_amount") or 0)))
            for t in price.get("tiers") or []]
    if have != _tiers(item):
        out.append(f"tiers {[(u, str(r)) for u, r in have]} != {[(u, str(r)) for u, r in _tiers(item)]}")
    return out


def _form(item: dict, product: str, meter_ids: dict) -> dict:
    f = {"product": product, "currency": item["currency"], "lookup_key": item["key"], "transfer_lookup_key": "true",
         "recurring[interval]": "month", "metadata[plan]": item["plan"]}
    if item["event"] is None:
        return {**f, "unit_amount": str(item["amount"])}
    f.update({"billing_scheme": "tiered", "tiers_mode": "graduated", "recurring[usage_type]": "metered",
              "recurring[meter]": meter_ids[item["event"]], "metadata[event]": item["event"]})
    for i, (up_to, rate) in enumerate(_tiers(item)):
        f[f"tiers[{i}][up_to]"] = "inf" if up_to is None else str(up_to)
        f[f"tiers[{i}][unit_amount_decimal]"] = format(rate, "f")
    return f


def _meters(api, events: set, apply: bool, drift: list) -> dict:
    have = {m["event_name"]: m for m in _ok(api("GET", "/billing/meters", {"limit": 100}))["data"]}
    ids = {}
    for ev in sorted(events):
        m = have.get(METERS[ev])
        if not m:
            drift.append(f"meter {METERS[ev]} missing")
            if apply:
                m = _ok(api("POST", "/billing/meters", {
                    "display_name": f"Agent {ev}", "event_name": METERS[ev], "default_aggregation[formula]": "sum",
                    "customer_mapping[event_payload_key]": "stripe_customer_id", "customer_mapping[type]": "by_id",
                    "value_settings[event_payload_key]": "value"}, idem=f"meter-{METERS[ev]}"))
        if m:
            ids[ev] = m["id"]
    return ids


def _product(api, plan: str) -> str:
    pid = f"agentic_{plan}"
    if api("GET", f"/products/{pid}").status_code == 404:
        _ok(api("POST", "/products", {"id": pid, "name": f"Agentic DevOps {plan}"}))
    return pid


def sync(apply: bool = False, api=None, plans: dict | None = None, environ=None) -> tuple[list[str], dict]:
    """Returns (drift, env). Without apply nothing is written. With apply, `drift` lists what was changed."""
    api = api or _http()
    environ = os.environ if environ is None else environ
    items = desired(plans)
    drift: list[str] = []
    meter_ids = _meters(api, {i["event"] for i in items if i["event"]}, apply, drift)
    found: dict[str, list[tuple[str, bool]]] = {}
    for it in items:
        rows = _ok(api("GET", "/prices", {"lookup_keys[]": [it["key"]], "active": "true", "expand[]": "data.tiers"}))["data"]
        price = rows[0] if rows else None
        issues = ["missing"] if not price else problems(it, price, meter_ids)
        if issues:
            drift.append(f"{it['key']}: {'; '.join(issues)}")
            if apply:
                form = _form(it, _product(api, it["plan"]), meter_ids)
                tag = hashlib.sha256(repr(sorted(form.items())).encode()).hexdigest()[:16]
                price = _ok(api("POST", "/prices", form, idem=f"price-{it['key']}-{tag}"))
        if price:
            found.setdefault(it["plan"], []).append((price["id"], it["event"] is not None))
    env = {f"STRIPE_METER_{e.upper()}": METERS[e] for e in sorted(meter_ids)}
    for plan, rows in found.items():
        var = f"STRIPE_PRICE_{plan.upper()}"
        env[var] = ",".join(pid + (":metered" if m else "") for pid, m in rows)
        complete = len(rows) == sum(1 for i in items if i["plan"] == plan)
        if not apply and complete and sorted(environ.get(var, "").split(",")) != sorted(env[var].split(",")):
            drift.append(f"{var} is {environ.get(var) or 'unset'}, catalog resolves to {env[var]}")
    if not apply:
        for e in sorted(meter_ids):
            if environ.get(f"STRIPE_METER_{e.upper()}", "") != METERS[e]:
                drift.append(f"STRIPE_METER_{e.upper()} should be {METERS[e]}")
    return drift, env


def main(argv=None) -> int:
    apply = "--apply" in (sys.argv[1:] if argv is None else argv)
    drift, env = sync(apply)
    for d in drift:
        print(("CHANGED " if apply else "DRIFT   ") + d)
    if not drift:
        print("in sync")
    if apply or not drift:
        print("\n# set these in the product's environment")
        print("\n".join(f"{k}={v}" for k, v in env.items()))
    return 1 if drift and not apply else 0


if __name__ == "__main__":
    sys.exit(main())
