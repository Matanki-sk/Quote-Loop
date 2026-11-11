"""All settings come from environment variables (see .env.example)."""
import os

from dotenv import load_dotenv

load_dotenv()


def _bool(name: str, default: bool = False) -> bool:
    return os.getenv(name, str(default)).strip().lower() in ("1", "true", "yes")


PAYPAL_BASE_URL = os.getenv("PAYPAL_BASE_URL", "https://api-m.sandbox.paypal.com").rstrip("/")
PAYPAL_CLIENT_ID = os.getenv("PAYPAL_CLIENT_ID", "")
PAYPAL_CLIENT_SECRET = os.getenv("PAYPAL_CLIENT_SECRET", "")
PAYPAL_WEBHOOK_ID = os.getenv("PAYPAL_WEBHOOK_ID", "")

# Development only: accept webhooks without verifying the PayPal signature.
SKIP_WEBHOOK_VERIFY = _bool("SKIP_WEBHOOK_VERIFY")

# Demo helper: send every invoice to this sandbox buyer email, whatever the
# customer's email in the request says (sandbox emails are not delivered anyway).
FORCE_RECIPIENT_EMAIL = os.getenv("FORCE_RECIPIENT_EMAIL", "").strip()

DATABASE_PATH = os.getenv("DATABASE_PATH", "quoteloop.db")

# Polling fallback: how often / how long we check PayPal after sending an invoice.
POLL_INTERVAL_SECONDS = int(os.getenv("POLL_INTERVAL_SECONDS", "5"))
POLL_MAX_TRIES = int(os.getenv("POLL_MAX_TRIES", "180"))
