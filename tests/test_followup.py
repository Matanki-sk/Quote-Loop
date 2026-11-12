"""Follow-up agent tests with the Groq and PayPal calls faked."""
import json
import os
import sys
import tempfile
import uuid

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("DATABASE_PATH", os.path.join(tempfile.mkdtemp(), "test_followup.db"))

from fastapi.testclient import TestClient  # noqa: E402

from app import db, llm, main, paypal  # noqa: E402
from app.pricing import build_quote  # noqa: E402

ITEMS = [{"catalog_id": "landing-page", "quantity": 3}, {"catalog_id": "maintenance", "quantity": 2}]


def add_invoice(pid, status="sent"):
    q = build_quote(ITEMS)
    db.insert_invoice(paypal_invoice_id=pid, invoice_number=pid[-4:], customer_name="Asha Rao",
                      customer_email="asha@example.com", currency="USD", total=q["total"],
                      items=q["lines"], flags=[], pay_url="https://example.test/pay", status=status)


def fake_chat(analysis, message="Hi Asha, thanks for your reply.", log=None):
    async def _chat(messages):
        if log is not None:
            log.append(messages)
        if "a customer's reply to an invoice" in messages[0]["content"]:
            return json.dumps(analysis)
        return json.dumps({"message": message})
    return _chat


def run(monkeypatch, pid, analysis, log=None):
    monkeypatch.setattr(llm, "_chat", fake_chat(analysis, log=log))
    with TestClient(main.app) as c:
        add_invoice(pid)
        r = c.post(f"/api/invoices/{pid}/followup", json={"text": "customer reply text"})
    assert r.status_code == 200, r.text
    return r.json()


def test_discount_above_cap_gets_counter(monkeypatch):
    log = []
    body = run(monkeypatch, "INV-F-0001",
               {"intent": "negotiate", "requested_discount_percent": 20, "summary": "Wants 20% off"}, log)
    d = body["decision"]
    assert d["action"] == "counter_at_cap" and d["tone"] == "warn"
    assert d["offer_pct"] == 15.0
    assert d["offer_total"] == 2295.0            # 2700 less 15%
    assert body["can_remind"] is True
    # the drafting prompt carries the code-computed numbers, not model math
    assert "$2,295.00" in log[1][0]["content"] and "15% off" in log[1][0]["content"]


def test_discount_within_cap_is_offered(monkeypatch):
    d = run(monkeypatch, "INV-F-0002",
            {"intent": "negotiate", "requested_discount_percent": 10, "summary": "x"})["decision"]
    assert d["action"] == "offer_within_policy" and d["offer_total"] == 2430.0


def test_no_number_asks_what_they_have_in_mind(monkeypatch):
    d = run(monkeypatch, "INV-F-0003", {"intent": "negotiate", "summary": "cheaper please"})["decision"]
    assert d["action"] == "ask" and d["offer_pct"] == 15.0


def test_accept_question_dispute(monkeypatch):
    assert run(monkeypatch, "INV-F-0004", {"intent": "accept", "summary": "ok"})["decision"]["action"] == "confirm"
    assert run(monkeypatch, "INV-F-0005", {"intent": "question", "summary": "q"})["decision"]["action"] == "answer"
    d = run(monkeypatch, "INV-F-0006", {"intent": "dispute", "summary": "no"})["decision"]
    assert d["action"] == "escalate" and d["tone"] == "stop"


def test_unknown_intent_becomes_review(monkeypatch):
    assert run(monkeypatch, "INV-F-0007", {"intent": "banana"})["decision"]["action"] == "review"


def test_unknown_invoice_is_404(monkeypatch):
    monkeypatch.setattr(llm, "_chat", fake_chat({"intent": "accept"}))
    with TestClient(main.app) as c:
        assert c.post("/api/invoices/NOPE/followup", json={"text": "hello there"}).status_code == 404


def test_reminder_sent_and_logged(monkeypatch):
    sent = {}

    async def fake_remind(pid, subject, note):
        sent.update(pid=pid, subject=subject, note=note)
        return {}

    monkeypatch.setattr(paypal, "remind_invoice", fake_remind)
    with TestClient(main.app) as c:
        add_invoice("INV-F-0008")
        r = c.post("/api/invoices/INV-F-0008/remind", json={"note": "Friendly reminder"})
        assert r.status_code == 200
        events = [e["type"] for e in c.get("/api/invoices/INV-F-0008/events").json()]
    assert sent["note"] == "Friendly reminder" and sent["subject"].startswith("Re: invoice #")
    assert events == ["reminder"]


def test_reminder_refused_for_paid_invoice(monkeypatch):
    with TestClient(main.app) as c:
        add_invoice("INV-F-0009", status="paid")
        r = c.post("/api/invoices/INV-F-0009/remind", json={"note": "Friendly reminder"})
    assert r.status_code == 400


def _fake_paypal(monkeypatch, cancel):
    async def number():
        return "0099"

    async def create(payload):
        return "INV-NEW-" + uuid.uuid4().hex[:6].upper()

    async def send(invoice_id):
        return {}

    async def get(invoice_id):
        return {"status": "SENT", "detail": {"metadata": {"recipient_view_url": "https://example.test/pay"}}}

    async def no_poll(paypal_id):
        return None

    monkeypatch.setattr(paypal, "generate_invoice_number", number)
    monkeypatch.setattr(paypal, "create_invoice", create)
    monkeypatch.setattr(paypal, "send_invoice", send)
    monkeypatch.setattr(paypal, "get_invoice", get)
    monkeypatch.setattr(paypal, "cancel_invoice", cancel)
    monkeypatch.setattr(main, "poll_invoice", no_poll)


def _revised_body(replaces):
    q = build_quote(ITEMS, override_discount=0.15)
    return {"customer_name": "Asha Rao", "customer_email": "asha@example.com", "quote": q, "replaces": replaces}


def test_revised_invoice_cancels_the_old_one(monkeypatch):
    cancelled = {}

    async def cancel(pid, subject, note):
        cancelled.update(pid=pid, subject=subject, note=note)
        return {}

    _fake_paypal(monkeypatch, cancel)
    with TestClient(main.app) as c:
        add_invoice("INV-OLD-0010")
        r = c.post("/api/invoices/approve", json=_revised_body("INV-OLD-0010"))
        assert r.status_code == 200, r.text
        assert r.json()["replaced"] == {"number": "0010", "cancelled": True}
        invs = {i["paypal_invoice_id"]: i for i in c.get("/api/invoices").json()}
        events = [e["type"] for e in c.get("/api/invoices/INV-OLD-0010/events").json()]
    new_id = r.json()["paypal_invoice_id"]
    assert invs["INV-OLD-0010"]["status"] == "cancelled" and invs[new_id]["total"] == 2295.0
    assert cancelled["pid"] == "INV-OLD-0010" and "#0099" in cancelled["subject"]
    assert events == ["cancelled"]


def test_failed_cancel_is_reported_not_fatal(monkeypatch):
    async def cancel(pid, subject, note):
        raise paypal.PayPalError("boom")

    _fake_paypal(monkeypatch, cancel)
    with TestClient(main.app) as c:
        add_invoice("INV-OLD-0011")
        r = c.post("/api/invoices/approve", json=_revised_body("INV-OLD-0011"))
    assert r.status_code == 200
    assert r.json()["replaced"]["cancelled"] is False and "boom" in r.json()["replaced"]["error"]


def test_paid_invoice_is_never_cancelled(monkeypatch):
    called = []

    async def cancel(pid, subject, note):
        called.append(pid)
        return {}

    _fake_paypal(monkeypatch, cancel)
    with TestClient(main.app) as c:
        add_invoice("INV-OLD-0012", status="paid")
        r = c.post("/api/invoices/approve", json=_revised_body("INV-OLD-0012"))
    assert r.status_code == 200 and r.json()["replaced"] is None and called == []