"""End-to-end check of quote -> approve -> webhook, with PayPal calls faked."""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ["DATABASE_PATH"] = os.path.join(tempfile.mkdtemp(), "test.db")
os.environ["SKIP_WEBHOOK_VERIFY"] = "true"

from fastapi.testclient import TestClient  # noqa: E402

from app import main, paypal  # noqa: E402


def test_full_flow(monkeypatch):
    sent = {}

    async def fake_number():
        return "0001"

    async def fake_create(payload):
        sent["payload"] = payload
        return "INV2-TEST-0001"

    async def fake_send(invoice_id):
        return {}

    async def fake_get(invoice_id):
        return {"status": "SENT", "detail": {"metadata": {"recipient_view_url": "https://example.test/pay"}}}

    async def no_poll(paypal_id):
        return None

    monkeypatch.setattr(paypal, "generate_invoice_number", fake_number)
    monkeypatch.setattr(paypal, "create_invoice", fake_create)
    monkeypatch.setattr(paypal, "send_invoice", fake_send)
    monkeypatch.setattr(paypal, "get_invoice", fake_get)
    monkeypatch.setattr(main, "poll_invoice", no_poll)
    monkeypatch.setattr(main.config, "SKIP_WEBHOOK_VERIFY", True)

    with TestClient(main.app) as client:
        q = client.post("/api/quote", json={
            "items": [{"catalog_id": "landing-page", "quantity": 3},
                      {"catalog_id": "maintenance", "quantity": 2}],
            "requested_discount": 0.25,
        }).json()
        assert q["total"] == 2580.0

        r = client.post("/api/invoices/approve", json={
            "customer_name": "Asha Rao", "customer_email": "asha@example.com", "quote": q})
        assert r.status_code == 200, r.text
        assert r.json()["status"] == "sent"

        items = sent["payload"]["items"]
        assert items[0]["quantity"] == "3" and items[0]["discount"] == {"percent": "5"}
        assert items[0]["unit_amount"]["value"] == "800.00"

        # PayPal tells us the invoice was paid
        w = client.post("/api/webhooks/paypal", json={
            "event_type": "INVOICING.INVOICE.PAID",
            "resource": {"invoice": {"id": "INV2-TEST-0001"}}})
        assert w.status_code == 200

        inv = client.get("/api/invoices").json()[0]
        assert inv["status"] == "paid" and inv["paid_at"]
        events = [e["type"] for e in client.get("/api/invoices/INV2-TEST-0001/events").json()]
        assert events == ["draft", "sent", "paid"]


def test_blocks_error_flags(monkeypatch):
    with TestClient(main.app) as client:
        q = client.post("/api/quote", json={"items": [{"catalog_id": "nope", "quantity": 1}]}).json()
        r = client.post("/api/invoices/approve", json={
            "customer_name": "X", "customer_email": "x@example.com", "quote": q})
        assert r.status_code == 400
