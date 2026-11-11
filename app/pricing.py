"""Rule-based pricing engine. Plain code, no LLM: the AI only picks catalog items
and quantities, and this module does all the money math and policy checks."""
import json
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path

RULES_PATH = Path(__file__).resolve().parent.parent / "pricing.json"


def load_rules(path: Path = RULES_PATH) -> dict:
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def _d(x) -> Decimal:
    return Decimal(str(x))


def _money(x: Decimal) -> Decimal:
    return x.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def _pct(x: Decimal) -> str:
    return f"{(x * 100).normalize():f}%"


def tier_discount(rules: dict, qty: int) -> Decimal:
    best = Decimal("0")
    for tier in rules["volumeTiers"]:
        if qty >= tier["minQty"] and _d(tier["discount"]) > best:
            best = _d(tier["discount"])
    return best


def build_quote(
    items: list[dict],
    requested_discount: float | None = None,
    deadline_days: int | None = None,
    override_discount: float | None = None,
    rules: dict | None = None,
) -> dict:
    """items: [{"catalog_id": "landing-page", "quantity": 3}, ...]

    requested_discount: what the customer asked for, e.g. 0.25 for 25%.
    override_discount: the owner's manual decision; beats everything else.
    deadline_days: days until the customer's deadline (None = no deadline).
    """
    rules = rules or load_rules()
    catalog = {c["id"]: c for c in rules["catalog"]}
    cap = _d(rules["maxAutoDiscount"])
    tax_rate = _d(rules.get("taxRate", 0))

    req = _d(requested_discount) if requested_discount is not None else None
    ovr = _d(override_discount) if override_discount is not None else None
    request_within_policy = req is not None and req <= cap

    flags: list[dict] = []
    trace: list[str] = []
    lines: list[dict] = []

    if req is not None and req > cap:
        flags.append({
            "code": "discount_above_cap",
            "severity": "warning",
            "message": f"Customer asked for {_pct(req)} off; policy allows up to {_pct(cap)}. "
                       f"Approve manually or counter at {_pct(cap)}.",
        })
        trace.append(f"Requested discount {_pct(req)} exceeds the {_pct(cap)} cap -> flagged, not applied")
    elif req is not None:
        trace.append(f"Requested discount {_pct(req)} is within the {_pct(cap)} cap")

    for it in items:
        cid = it.get("catalog_id")
        try:
            qty = int(it.get("quantity", 0))
        except (TypeError, ValueError):
            qty = 0
        item = catalog.get(cid)
        if item is None:
            flags.append({"code": "item_not_in_catalog", "severity": "error",
                          "message": f"'{cid}' is not in the catalog. Add it manually or drop it."})
            trace.append(f"'{cid}' not found in catalog -> flagged")
            continue
        if qty <= 0:
            flags.append({"code": "bad_quantity", "severity": "error",
                          "message": f"Quantity for '{item['name']}' must be at least 1."})
            continue

        unit = _d(item["price"])
        gross = _money(unit * qty)
        tier = tier_discount(rules, qty)
        pct, reason = tier, ("volume tier" if tier > 0 else "none")
        if ovr is not None:
            pct, reason = ovr, "owner override"
        elif request_within_policy and req > pct:
            pct, reason = req, "customer request within policy"
        discount = _money(gross * pct)

        trace.append(f"Matched '{cid}' -> {item['name']} (${unit}/{item['unit']}) x {qty}")
        if pct > 0:
            trace.append(f"  {item['name']}: {_pct(pct)} off ({reason})")

        lines.append({
            "kind": "item",
            "catalog_id": cid,
            "name": item["name"],
            "description": f"{qty} x {item['name']} ({item['unit']})",
            "quantity": qty,
            "unit_price": float(unit),
            "gross": float(gross),
            "discount_pct": float(pct * 100),
            "discount_reason": reason,
            "discount_amount": float(discount),
            "line_total": float(gross - discount),
            "tax_percent": float(tax_rate * 100),
        })

    subtotal = sum((_d(l["gross"]) for l in lines), Decimal("0"))
    discount_total = sum((_d(l["discount_amount"]) for l in lines), Decimal("0"))
    net = subtotal - discount_total

    surcharge = Decimal("0")
    if lines and deadline_days is not None and deadline_days < rules["rushSurchargeDays"]:
        rate = _d(rules["rushSurcharge"])
        surcharge = _money(net * rate)
        flags.append({
            "code": "rush_deadline",
            "severity": "info",
            "message": f"Deadline in {deadline_days} day(s) is under {rules['rushSurchargeDays']}; "
                       f"added a {_pct(rate)} rush surcharge.",
        })
        trace.append(f"Deadline in {deadline_days} day(s) -> rush surcharge {_pct(rate)} = ${surcharge}")
        lines.append({
            "kind": "surcharge",
            "catalog_id": None,
            "name": "Rush surcharge",
            "description": f"{_pct(rate)} rush fee (deadline under {rules['rushSurchargeDays']} days)",
            "quantity": 1,
            "unit_price": float(surcharge),
            "gross": float(surcharge),
            "discount_pct": 0.0,
            "discount_reason": "none",
            "discount_amount": 0.0,
            "line_total": float(surcharge),
            "tax_percent": float(tax_rate * 100),
        })

    taxable = net + surcharge
    tax = _money(taxable * tax_rate)
    total = taxable + tax

    return {
        "business": rules["business"],
        "currency": rules["currency"],
        "payment_term": rules["paymentTerm"],
        "lines": lines,
        "subtotal": float(subtotal),
        "discount_total": float(discount_total),
        "surcharge": float(surcharge),
        "tax": float(tax),
        "total": float(total),
        "flags": flags,
        "trace": trace,
    }
