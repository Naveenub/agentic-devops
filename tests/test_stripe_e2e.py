"""The e2e runner's own safety and plumbing; the Stripe-facing part runs only via `make stripe-e2e`."""
from fastapi.testclient import TestClient

from product import stripe_e2e, store


def test_refuses_live_and_missing_keys(monkeypatch, capsys):
    for k in ("sk_live_abc", "", "pk_test_abc"):
        monkeypatch.setenv("STRIPE_SECRET_KEY", k)
        assert stripe_e2e.main([]) == 2
    assert "test-mode" in capsys.readouterr().out


def test_deliver_signs_for_the_real_webhook_route(tmp_path, monkeypatch):
    monkeypatch.setenv("PRODUCT_DB", str(tmp_path / "p.db"))
    monkeypatch.setenv("PRODUCT_ADMIN_KEY", "adm")
    monkeypatch.setenv("AGENT_LLM", "off")
    monkeypatch.setenv("STRIPE_WEBHOOK_SECRET", stripe_e2e.LOCAL_WHSEC)
    from api.main import app
    t = store.create_tenant("t")
    ev = {"id": "evt_1", "type": "customer.subscription.deleted",
          "data": {"object": {"id": "sub_1", "customer": "cus_1", "metadata": {"tenant_id": t["id"]}}}}
    c = TestClient(app)
    assert stripe_e2e.deliver(c, ev) == "200:handled"
    assert stripe_e2e.deliver(c, ev) == "200:duplicate"
