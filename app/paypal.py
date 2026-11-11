"""Thin async client for the PayPal Invoicing API (sandbox by default)."""
import time
from datetime import date

import httpx

from . import config


class PayPalError(Exception):
    pass


_token = {"value": None, "expires_at": 0.0}


async def get_token() -> str:
    """OAuth2 client-credentials token, cached until shortly before it expires."""
    if _token["value"] and time.time() < _token["expires_at"] - 60:
        return _token["value"]
    if not config.PAYPAL_CLIENT_ID or not config.PAYPAL_CLIENT_SECRET:
        raise PayPalError("PAYPAL_CLIENT_ID / PAYPAL_CLIENT_SECRET are not set (see .env.example)")
    async with httpx.AsyncClient(base_url=config.PAYPAL_BASE_URL, timeout=30) as c:
        r = await c.post(
            "/v1/oauth2/token",
            auth=(config.PAYPAL_CLIENT_ID, config.PAYPAL_CLIENT_SECRET),
            data={"grant_type": "client_credentials"},
        )
    if r.status_code != 200:
        raise PayPalError(f"Token request failed: {r.status_code} {r.text}")
    data = r.json()
    _token["value"] = data["access_token"]
    _token["expires_at"] = time.time() + int(data.get("expires_in", 300))
    return _token["value"]


async def _request(method: str, path: str, json: dict | None = None) -> dict:
    token = await get_token()
    async with httpx.AsyncClient(base_url=config.PAYPAL_BASE_URL, timeout=30) as c:
        r = await c.request(
            method, path, json=json,
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        )
    if r.status_code >= 400:
        raise PayPalError(f"{method} {path} -> {r.status_code}: {r.text}")
    return r.json() if r.content else {}


async def generate_invoice_number() -> str:
    data = await _request("POST", "/v2/invoicing/generate-next-invoice-number")
    return data["invoice_number"]


def build_invoice_payload(
    *,
    invoice_number: str,
    customer_name: str,
    customer_email: str,
    lines: list[dict],
    currency: str = "USD",
    note: str = "",
    payment_term: str = "NET_14",
) -> dict:
    """Turn our quote lines (see pricing.build_quote) into a PayPal invoice body."""
    parts = customer_name.strip().split(" ", 1)
    name = {"given_name": parts[0] or "Customer"}
    if len(parts) > 1 and parts[1].strip():
        name["surname"] = parts[1].strip()

    items = []
    for ln in lines:
        item = {
            "name": ln["name"][:200],
            "description": ln.get("description", "")[:1000],
            "quantity": str(ln["quantity"]),
            "unit_amount": {"currency_code": currency, "value": f"{ln['unit_price']:.2f}"},
            "unit_of_measure": "QUANTITY",
        }
        if ln.get("discount_pct", 0) > 0:
            item["discount"] = {"percent": f"{ln['discount_pct']:g}"}
        if ln.get("tax_percent", 0) > 0:
            item["tax"] = {"name": "Tax", "percent": f"{ln['tax_percent']:g}"}
        items.append(item)

    detail = {
        "invoice_number": invoice_number,
        "invoice_date": date.today().isoformat(),
        "currency_code": currency,
        "payment_term": {"term_type": payment_term},
    }
    if note:
        detail["note"] = note[:4000]

    return {
        "detail": detail,
        "primary_recipients": [
            {"billing_info": {"name": name, "email_address": customer_email}}
        ],
        "items": items,
        "configuration": {"allow_tip": False, "tax_inclusive": False},
    }


async def create_invoice(payload: dict) -> str:
    """Creates a DRAFT invoice and returns its PayPal id (e.g. INV2-XXXX-...)."""
    data = await _request("POST", "/v2/invoicing/invoices", json=payload)
    href = data.get("href", "")
    if not href:
        raise PayPalError(f"Unexpected create-invoice response: {data}")
    return href.rstrip("/").rsplit("/", 1)[-1]


async def send_invoice(invoice_id: str) -> dict:
    return await _request(
        "POST", f"/v2/invoicing/invoices/{invoice_id}/send",
        json={"send_to_recipient": True, "send_to_invoicer": False},
    )


async def get_invoice(invoice_id: str) -> dict:
    return await _request("GET", f"/v2/invoicing/invoices/{invoice_id}")


async def remind_invoice(invoice_id: str, subject: str, note: str) -> dict:
    return await _request(
        "POST", f"/v2/invoicing/invoices/{invoice_id}/remind",
        json={"subject": subject, "note": note, "send_to_invoicer": False},
    )


async def verify_webhook(headers, event: dict) -> bool:
    """Ask PayPal whether this webhook call is genuine."""
    body = {
        "auth_algo": headers.get("paypal-auth-algo"),
        "cert_url": headers.get("paypal-cert-url"),
        "transmission_id": headers.get("paypal-transmission-id"),
        "transmission_sig": headers.get("paypal-transmission-sig"),
        "transmission_time": headers.get("paypal-transmission-time"),
        "webhook_id": config.PAYPAL_WEBHOOK_ID,
        "webhook_event": event,
    }
    if not all(body[k] for k in body if k != "webhook_event"):
        return False
    data = await _request("POST", "/v1/notifications/verify-webhook-signature", json=body)
    return data.get("verification_status") == "SUCCESS"
