"""Extraction tests with the Groq call faked, so no API key or network is needed."""
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("DATABASE_PATH", os.path.join(tempfile.mkdtemp(), "test_extract.db"))

from fastapi.testclient import TestClient  # noqa: E402

from app import llm, main  # noqa: E402

GOOD = {
    "customer_name": "Asha Rao",
    "customer_email": "asha@example.com",
    "items": [
        {"catalog_id": "landing-page", "requested_name": "landing pages", "quantity": 3, "confidence": "high"},
        {"catalog_id": "maintenance", "requested_name": "maintenance", "quantity": 2, "confidence": "high"},
    ],
    "deadline_days": 10,
    "requested_discount_percent": 25,
    "summary": "3 landing pages and 2 months of maintenance, asks for 25% off.",
}


def fake_chat(responses):
    calls = {"n": 0}

    async def _chat(messages):
        r = responses[min(calls["n"], len(responses) - 1)]
        calls["n"] += 1
        return r if isinstance(r, str) else json.dumps(r)

    _chat.calls = calls
    return _chat


def test_extract_endpoint_prices_and_flags(monkeypatch):
    monkeypatch.setattr(llm, "_chat", fake_chat([GOOD]))
    with TestClient(main.app) as client:
        res = client.post("/api/extract", json={"text": "need 3 landing pages and 2 months maintenance, 25% off"})
    assert res.status_code == 200
    body = res.json()
    assert body["extracted"]["requested_discount"] == 0.25
    q = body["quote"]
    assert q["total"] == 2580.0
    assert [f["code"] for f in q["flags"]] == ["discount_above_cap"]
    assert q["trace"][0].startswith("Read the message")


def test_retry_on_bad_json(monkeypatch):
    chat = fake_chat(["this is not json", GOOD])
    monkeypatch.setattr(llm, "_chat", chat)
    with TestClient(main.app) as client:
        res = client.post("/api/extract", json={"text": "hello there"})
    assert res.status_code == 200
    assert chat.calls["n"] == 2


def test_gives_up_after_two_bad_responses(monkeypatch):
    monkeypatch.setattr(llm, "_chat", fake_chat(["nope", "still nope"]))
    with TestClient(main.app) as client:
        res = client.post("/api/extract", json={"text": "hello there"})
    assert res.status_code == 502


def test_unknown_and_low_confidence_items_are_flagged(monkeypatch):
    raw = {
        "items": [
            {"catalog_id": None, "requested_name": "Mobile App", "quantity": 1, "confidence": "high"},
            {"catalog_id": "logo", "requested_name": "a logo", "quantity": 1, "confidence": "low"},
        ],
        "customer_email": "not-an-email",
    }
    monkeypatch.setattr(llm, "_chat", fake_chat([raw]))
    with TestClient(main.app) as client:
        body = client.post("/api/extract", json={"text": "app and a logo please"}).json()
    codes = [f["code"] for f in body["quote"]["flags"]]
    assert "item_not_in_catalog" in codes and "low_confidence" in codes
    assert body["extracted"]["customer_email"] is None
    assert body["quote"]["total"] == 400.0


def test_no_items_blocks_with_error_flag(monkeypatch):
    monkeypatch.setattr(llm, "_chat", fake_chat([{"items": []}]))
    with TestClient(main.app) as client:
        body = client.post("/api/extract", json={"text": "just saying hi"}).json()
    assert any(f["code"] == "no_items_found" and f["severity"] == "error" for f in body["quote"]["flags"])


def test_missing_key_is_clear_error(monkeypatch):
    monkeypatch.setattr(llm.config, "GROQ_API_KEY", "")
    with TestClient(main.app) as client:
        res = client.post("/api/extract", json={"text": "hello there"})
    assert res.status_code == 500
    assert "GROQ_API_KEY" in res.json()["detail"]
