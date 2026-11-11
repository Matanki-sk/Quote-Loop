"""Reads a customer's message and turns it into structured data using Groq.

The model only decides WHICH catalog items and quantities the customer wants.
All prices, discounts and policy checks happen in app/pricing.py, never here.
"""
import json
import re
from datetime import date

import httpx

from . import config
from .pricing import load_rules

GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


class ExtractionError(Exception):
    def __init__(self, message: str, status: int = 502):
        super().__init__(message)
        self.status = status


PROMPT_TEMPLATE = """You extract a structured quote request from a customer's message for __BUSINESS__.
Today is __WEEKDAY__, __DATE__.

Catalog (use ONLY these ids):
__CATALOG__

Return ONLY a JSON object with exactly these keys:
{
  "customer_name": string or null,
  "customer_email": string or null,
  "items": [
    {"catalog_id": string or null, "requested_name": string, "quantity": integer,
     "confidence": "high" or "medium" or "low"}
  ],
  "deadline_days": integer or null,
  "requested_discount_percent": number or null,
  "summary": string
}

Rules:
- Map each requested service to the closest catalog id. If nothing in the catalog fits,
  set catalog_id to null and put what the customer asked for in requested_name.
- quantity: how many units. "a couple" = 2. For a monthly service wanted for N months, quantity is N.
  If no quantity is stated, use 1 and set confidence to "low".
- confidence: "high" = clearly stated and clearly matches the catalog, "medium" = reasonable guess,
  "low" = vague or guessed.
- deadline_days: whole days from today until the customer's deadline ("by Friday" = days until the
  next Friday, "next week" = 7, "ASAP" or "urgent" = 1). null if there is no deadline.
- requested_discount_percent: the percent discount the customer asks for, as a plain number
  (25 means 25 percent). null if they do not ask. A budget on its own is not a discount request.
- Never invent a name or email. Use null when it is not in the message.
- summary: one short sentence describing the request.
"""


def build_system_prompt(rules: dict, today: date) -> str:
    catalog = "\n".join(
        f"- {c['id']}: {c['name']} (${c['price']} per {c['unit']})" for c in rules["catalog"]
    )
    return (
        PROMPT_TEMPLATE
        .replace("__BUSINESS__", rules["business"])
        .replace("__WEEKDAY__", today.strftime("%A"))
        .replace("__DATE__", today.isoformat())
        .replace("__CATALOG__", catalog)
    )


async def _chat(messages: list[dict]) -> str:
    if not config.GROQ_API_KEY:
        raise ExtractionError("GROQ_API_KEY is not set (see .env.example)", status=500)
    async with httpx.AsyncClient(timeout=60) as c:
        r = await c.post(
            GROQ_URL,
            headers={"Authorization": f"Bearer {config.GROQ_API_KEY}"},
            json={
                "model": config.GROQ_MODEL,
                "messages": messages,
                "temperature": 0,
                "response_format": {"type": "json_object"},
            },
        )
    if r.status_code != 200:
        raise ExtractionError(f"Groq error {r.status_code}: {r.text}")
    return r.json()["choices"][0]["message"]["content"]


def _normalize(raw: dict) -> dict:
    """Clean up whatever the model returned so the rest of the app can trust it."""
    if not isinstance(raw, dict):
        raise ValueError("model did not return a JSON object")

    items = []
    for it in raw.get("items") or []:
        name = str(it.get("requested_name") or "").strip()
        cid = it.get("catalog_id")
        if not cid:
            cid = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-") or "unknown"
        try:
            qty = int(it.get("quantity") or 1)
        except (TypeError, ValueError):
            qty = 1
        conf = it.get("confidence")
        items.append({
            "catalog_id": str(cid),
            "requested_name": name,
            "quantity": max(qty, 1),
            "confidence": conf if conf in ("high", "medium", "low") else "medium",
        })

    discount = None
    try:
        pct = float(raw.get("requested_discount_percent"))
        if pct > 0:
            discount = round(pct / 100, 4)
    except (TypeError, ValueError):
        pass

    deadline_days = None
    try:
        d = int(raw.get("deadline_days"))
        if d >= 0:
            deadline_days = d
    except (TypeError, ValueError):
        pass

    email = raw.get("customer_email")
    email = email.strip() if isinstance(email, str) and EMAIL_RE.match(email.strip()) else None
    name = raw.get("customer_name")
    name = name.strip() if isinstance(name, str) and name.strip() else None

    return {
        "customer_name": name,
        "customer_email": email,
        "items": items,
        "deadline_days": deadline_days,
        "requested_discount": discount,
        "summary": str(raw.get("summary") or "").strip(),
    }


async def extract_request(text: str, rules: dict | None = None) -> dict:
    rules = rules or load_rules()
    messages = [
        {"role": "system", "content": build_system_prompt(rules, date.today())},
        {"role": "user", "content": text},
    ]
    last_error: Exception | None = None
    for _ in range(2):  # one retry if the model returns bad JSON
        content = await _chat(messages)
        try:
            return _normalize(json.loads(content))
        except (ValueError, TypeError, AttributeError) as e:
            last_error = e
            messages += [
                {"role": "assistant", "content": content},
                {"role": "user", "content": "That was not valid. Return ONLY the JSON object described above."},
            ]
    raise ExtractionError(f"Model returned invalid JSON twice: {last_error}")
