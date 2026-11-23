"""plans.py is the only place plan terms live; stripe_sync provisions Stripe from it and reports drift."""
import json
from decimal import Decimal

from product import plans, stripe_sync


class _R:
    def __init__(self, body, status=200):
        self.body, self.status_code, self.text = body, status, json.dumps(body)

    def json(self):
        return self.body


class FakeStripe:
    """Just enough of Stripe: meters, products, prices with lookup keys (transfer_lookup_key deactivates the old one)."""

    def __init__(self):
        self.meters, self.products, self.prices, self.n = {}, set(), [], 0

    def __call__(self, method, path, data=None, idem=None):
        data = data or {}
        if path == "/billing/meters" and method == "GET":
            return _R({"data": list(self.meters.values())})
        if path == "/billing/meters":
            self.n += 1
            self.meters[data["event_name"]] = {"id": f"mtr_{self.n}", "event_name": data["event_name"]}
            return _R(self.meters[data["event_name"]])
        if path.startswith("/products/"):
            return _R({}, 200 if path.rsplit("/", 1)[-1] in self.products else 404)
        if path == "/products":
            self.products.add(data["id"])
            return _R({"id": data["id"]})
        if path == "/prices" and method == "GET":
            key = data["lookup_keys[]"][0]
            return _R({"data": [p for p in self.prices if p["lookup_key"] == key and p["active"]]})
        if path == "/prices":
            self.n += 1
            for p in self.prices:
                if p["lookup_key"] == data["lookup_key"]:
                    p["active"] = False
            tiers = []
            i = 0
            while f"tiers[{i}][unit_amount_decimal]" in data:
                up = data[f"tiers[{i}][up_to]"]
                tiers.append({"up_to": None if up == "inf" else int(up),
                              "unit_amount_decimal": data[f"tiers[{i}][unit_amount_decimal]"]})
                i += 1
            price = {"id": f"price_{self.n}", "active": True, "lookup_key": data["lookup_key"], "currency": data["currency"],
                     "billing_scheme": data.get("billing_scheme", "per_unit"), "tiers_mode": data.get("tiers_mode"),
                     "unit_amount": int(data["unit_amount"]) if "unit_amount" in data else None, "tiers": tiers,
                     "recurring": {"interval": data["recurring[interval]"], "interval_count": 1,
                                   "usage_type": data.get("recurring[usage_type]", "licensed"),
                                   "meter": data.get("recurring[meter]")}}
            self.prices.append(price)
            return _R(price)
        raise AssertionError((method, path))


def _env_for(env: dict) -> dict:
    return dict(env)


def test_apply_provisions_everything_then_check_is_clean():
    api = FakeStripe()
    drift, env = stripe_sync.sync(apply=True, api=api)
    assert any("missing" in d for d in drift)
    assert set(env) >= {"STRIPE_PRICE_STARTER", "STRIPE_PRICE_PRO", "STRIPE_METER_ANALYZE"}
    assert "STRIPE_PRICE_ENTERPRISE" not in env and "STRIPE_PRICE_FREE" not in env
    drift, _ = stripe_sync.sync(apply=False, api=api, environ=_env_for(env))
    assert drift == []
    n = len(api.prices)
    stripe_sync.sync(apply=True, api=api)
    assert len(api.prices) == n  # re-running creates nothing


def test_included_units_in_stripe_follow_the_quota_in_plans_py(monkeypatch):
    api = FakeStripe()
    _, env = stripe_sync.sync(apply=True, api=api)
    starter = next(p for p in api.prices if p["lookup_key"] == "agentic_starter_analyze")
    assert starter["tiers"][0] == {"up_to": 1000, "unit_amount_decimal": "0"}
    assert Decimal(starter["tiers"][1]["unit_amount_decimal"]) == Decimal("5")
    monkeypatch.setenv("PRODUCT_PLANS", '{"starter": {"monthly_quota": {"analyze": 2000}}}')
    drift, _ = stripe_sync.sync(apply=False, api=api, environ=_env_for(env))
    assert len(drift) == 1 and drift[0].startswith("agentic_starter_analyze: tiers")


def test_changed_price_replaces_the_stripe_price_and_env_must_follow(monkeypatch):
    api = FakeStripe()
    _, env = stripe_sync.sync(apply=True, api=api)
    monkeypatch.setenv("PRODUCT_PLANS", '{"pro": {"base_cents": 12900}}')
    drift, _ = stripe_sync.sync(apply=False, api=api, environ=_env_for(env))
    assert drift == ["agentic_pro_base: amount 9900 != 12900"]
    drift, new_env = stripe_sync.sync(apply=True, api=api)
    assert drift == ["agentic_pro_base: amount 9900 != 12900"] and new_env["STRIPE_PRICE_PRO"] != env["STRIPE_PRICE_PRO"]
    assert stripe_sync.sync(apply=False, api=api, environ=_env_for(env))[0] == [
        f"STRIPE_PRICE_PRO is {env['STRIPE_PRICE_PRO']}, catalog resolves to {new_env['STRIPE_PRICE_PRO']}"]
    assert stripe_sync.sync(apply=False, api=api, environ=_env_for(new_env))[0] == []


def test_hand_edited_stripe_price_is_reported():
    api = FakeStripe()
    _, env = stripe_sync.sync(apply=True, api=api)
    next(p for p in api.prices if p["lookup_key"] == "agentic_starter_base")["unit_amount"] = 5900
    assert stripe_sync.sync(apply=False, api=api, environ=_env_for(env))[0] == ["agentic_starter_base: amount 5900 != 4900"]


def test_unset_meter_env_is_drift():
    api = FakeStripe()
    _, env = stripe_sync.sync(apply=True, api=api)
    env.pop("STRIPE_METER_LLM_TOKENS")
    assert stripe_sync.sync(apply=False, api=api, environ=_env_for(env))[0] == [
        f"STRIPE_METER_LLM_TOKENS should be {plans.METERS['llm_tokens']}"]


def test_admin_drift_endpoint(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from api.main import app
    monkeypatch.setenv("PRODUCT_DB", str(tmp_path / "p.db"))
    monkeypatch.setenv("PRODUCT_ADMIN_KEY", "adm")
    monkeypatch.setenv("STRIPE_SECRET_KEY", "sk_test")
    api = FakeStripe()
    monkeypatch.setattr(stripe_sync, "_http", lambda: api)
    c = TestClient(app)
    assert c.get("/admin/billing/drift").status_code == 401
    r = c.get("/admin/billing/drift", headers={"x-admin-key": "adm"}).json()
    assert r["in_sync"] is False and any("missing" in d for d in r["drift"])


def test_seat_price_is_a_flat_licensed_price_flagged_in_the_env(monkeypatch):
    api = FakeStripe()
    drift, env = stripe_sync.sync(apply=True, api=api)
    assert "agentic_pro_seat: missing" in drift and "agentic_starter_seat: missing" in drift
    seat = next(p for p in api.prices if p["lookup_key"] == "agentic_pro_seat")
    assert seat["unit_amount"] == 2500 and seat["billing_scheme"] == "per_unit" and seat["recurring"]["usage_type"] == "licensed"
    assert env["STRIPE_PRICE_PRO"].count(":seat") == 1 and ":seat" in env["STRIPE_PRICE_STARTER"]
    assert "free" not in {p["lookup_key"].split("_")[1] for p in api.prices}  # no seat price for a plan without seat_cents
    assert stripe_sync.sync(apply=False, api=api, environ=_env_for(env))[0] == []
    # the env the sync prints is what checkout and the seat sync read back
    from product import subscriptions
    monkeypatch.setenv("STRIPE_PRICE_PRO", env["STRIPE_PRICE_PRO"])
    seat_id = next(p["id"] for p in api.prices if p["lookup_key"] == "agentic_pro_seat")
    assert subscriptions.seat_price("pro") == seat_id and seat_id not in [pid for pid, _ in subscriptions.prices_for("pro")]
    assert subscriptions.plan_for_prices([seat_id]) is None  # the seat price alone never decides the plan
    monkeypatch.setenv("PRODUCT_PLANS", '{"pro": {"seat_cents": 3000}}')
    assert stripe_sync.sync(apply=False, api=api, environ=_env_for(env))[0] == ["agentic_pro_seat: amount 2500 != 3000"]
    monkeypatch.setenv("PRODUCT_PLANS", '{"pro": {"seat_cents": null}}')  # seats switched off for the plan
    assert "agentic_pro_seat" not in [i["key"] for i in stripe_sync.desired()]
