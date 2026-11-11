"""Checkpoint 1: create and send a hardcoded invoice through the PayPal sandbox.

Run from the project folder:   python scripts/send_test_invoice.py
Then open the printed link, log in as your sandbox PERSONAL (buyer) account and pay.
"""
import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import config, paypal  # noqa: E402
from app.pricing import build_quote  # noqa: E402


async def main() -> None:
    buyer_email = config.FORCE_RECIPIENT_EMAIL
    if not buyer_email:
        sys.exit("Set FORCE_RECIPIENT_EMAIL in .env to your sandbox PERSONAL account email.")

    quote = build_quote([{"catalog_id": "landing-page", "quantity": 3},
                         {"catalog_id": "maintenance", "quantity": 2}])
    print(f"Quote total: {quote['currency']} {quote['total']:.2f}")

    number = await paypal.generate_invoice_number()
    payload = paypal.build_invoice_payload(
        invoice_number=number,
        customer_name="Test Customer",
        customer_email=buyer_email,
        lines=quote["lines"],
        currency=quote["currency"],
        note="Thanks for your business! (QuoteLoop test invoice)",
        payment_term=quote["payment_term"],
    )
    invoice_id = await paypal.create_invoice(payload)
    print("Created draft:", invoice_id)

    await paypal.send_invoice(invoice_id)
    inv = await paypal.get_invoice(invoice_id)
    print("Status:", inv.get("status"))
    print("Pay link:", inv.get("detail", {}).get("metadata", {}).get("recipient_view_url"))


if __name__ == "__main__":
    asyncio.run(main())
