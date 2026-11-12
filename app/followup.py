"""Follow-up agent: reads a customer's reply to an invoice, decides what to do with it
using the pricing rules (plain code), and drafts a response.

The LLM only (1) classifies the reply and (2) words the answer. Whether a discount is
allowed, and every number in the answer, come from code, never from the model.
"""
from . import llm
from .pricing import build_quote, load_rules

CLOSED_STATUSES = {"draft", "paid", "marked_as_paid", "cancelled", "refunded", "marked_as_refunded"}


def _money(x: float) -> str:
    return f"${x:,.2f}"


def _pct(fraction: float) -> str:
    return f"{round(fraction * 100, 2):g}%"


def _item_lines(inv: dict) -> list[dict]:
    return [l for l in inv["items"] if l.get("kind") == "item"]


def _blended_discount(inv: dict) -> float:
    lines = _item_lines(inv)
    gross = sum(l["gross"] for l in lines)
    return (sum(l["discount_amount"] for l in lines) / gross) if gross else 0.0


def _offer_total(inv: dict, pct: float, rules: dict) -> float:
    """Total the invoice would have at this discount, using the same pricing engine."""
    items = [{"catalog_id": l["catalog_id"], "quantity": l["quantity"]} for l in _item_lines(inv)]
    rush = any(l.get("kind") == "surcharge" for l in inv["items"])
    return build_quote(items, override_discount=pct, deadline_days=0 if rush else None, rules=rules)["total"]


def _decision(action, tone, title, note, offer_pct=None, offer_total=None) -> dict:
    return {
        "action": action, "tone": tone, "title": title, "note": note,
        "offer_pct": None if offer_pct is None else round(offer_pct * 100, 2),
        "offer_total": offer_total,
    }


def decide(inv: dict, analysis: dict, rules: dict) -> dict:
    intent = analysis["intent"]
    req = analysis["requested_discount"]
    cap = float(rules["maxAutoDiscount"])

    if intent == "accept":
        return _decision("confirm", "ok", "Customer agrees",
                         "Nothing to change. Send a short confirmation, or a reminder if it is still unpaid.")

    if intent == "negotiate":
        have = _blended_discount(inv)
        if req is not None and req <= have + 1e-9:
            return _decision("already_applied", "info", "Already covered",
                             f"The invoice already includes {_pct(have)} off. No change is needed.")
        if req is None:
            total = _offer_total(inv, cap, rules)
            return _decision("ask", "info", "Customer wants a better price",
                             f"They gave no number. You can offer up to {_pct(cap)} (total {_money(total)}).",
                             cap, total)
        if req <= cap:
            total = _offer_total(inv, req, rules)
            return _decision("offer_within_policy", "ok", "Discount within your limit",
                             f"They asked for {_pct(req)} and your limit is {_pct(cap)}, so you can agree. "
                             f"The total would be {_money(total)}. You can prepare a revised quote below.",
                             req, total)
        total = _offer_total(inv, cap, rules)
        return _decision("counter_at_cap", "warn", "Above your discount limit",
                         f"They asked for {_pct(req)} but your limit is {_pct(cap)}. "
                         f"A counter at {_pct(cap)} would make the total {_money(total)}.",
                         cap, total)

    if intent == "question":
        return _decision("answer", "info", "Customer has a question",
                         "The draft answers from the invoice details only. Check it before sending.")
    if intent == "dispute":
        return _decision("escalate", "stop", "Customer disputes the invoice",
                         "Don't offer anything yet. Read the draft, then reply yourself.")
    return _decision("review", "info", "Needs a look",
                     "The agent could not tell what they want. Read the message and reply yourself.")


STANCES = {
    "confirm": "Thank the customer and confirm. Say the invoice can be paid from the PayPal link already sent.",
    "already_applied": "Explain that the invoice already includes the volume discount, so the total stays as it is. Thank them.",
    "ask": "Ask what discount or budget they have in mind. Say you can offer up to {offer_pct}% off, "
           "which would make the total {offer_total}.",
    "offer_within_policy": "Agree to {offer_pct}% off, which makes the total {offer_total}. "
                           "Say a revised invoice will follow.",
    "counter_at_cap": "Politely say the requested discount is more than you can offer. "
                      "Offer {offer_pct}% off instead, which makes the total {offer_total}. "
                      "Say a revised invoice will follow if they accept.",
    "answer": "Answer the customer's question using only the facts above. "
              "If the facts do not cover it, say you will check and get back to them.",
    "escalate": "Acknowledge their concern and say you are looking into it and will reply shortly. "
                "Do not promise refunds, discounts or changes.",
    "review": "Acknowledge the message and say you will get back to them shortly.",
}


def build_draft_prompt(inv: dict, decision: dict, rules: dict) -> str:
    items = ", ".join(f"{l['quantity']} x {l['name']}" for l in _item_lines(inv)) or "n/a"
    stance = STANCES[decision["action"]].format(
        offer_pct=f"{decision['offer_pct']:g}" if decision["offer_pct"] is not None else "",
        offer_total=_money(decision["offer_total"]) if decision["offer_total"] is not None else "",
    )
    return (
        f"You write a short, friendly, professional reply from {rules['business']} to a customer "
        f"about an invoice.\n\nFacts (use only these; copy numbers exactly; never do your own math):\n"
        f"- Customer: {inv['customer_name']}\n"
        f"- Invoice number: {inv['invoice_number']}\n"
        f"- Invoice total: {_money(inv['total'])}\n"
        f"- Items: {items}\n"
        f"- Payment terms: {rules['paymentTerm'].replace('_', ' ').title()}\n"
        f"- Invoice status: {inv['status']}\n\n"
        f"What to say: {stance}\n\n"
        f"Return ONLY a JSON object: {{\"message\": \"...\"}}. At most 90 words. Plain text, no subject line, "
        f"sign off as {rules['business']}."
    )


async def handle_reply(inv: dict, text: str) -> dict:
    rules = load_rules()
    analysis = await llm.analyze_reply(text, rules["business"])
    decision = decide(inv, analysis, rules)
    draft = await llm.draft_reply(build_draft_prompt(inv, decision, rules), text)
    return {
        "intent": analysis["intent"],
        "summary": analysis["summary"],
        "requested_discount": analysis["requested_discount"],
        "decision": decision,
        "draft": draft,
        "can_remind": inv["status"] not in CLOSED_STATUSES,
    }