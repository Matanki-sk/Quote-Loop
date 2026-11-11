import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.pricing import build_quote  # noqa: E402

ITEMS = [{"catalog_id": "landing-page", "quantity": 3},
         {"catalog_id": "maintenance", "quantity": 2}]


def codes(q):
    return [f["code"] for f in q["flags"]]


def test_worked_example_discount_above_cap():
    q = build_quote(ITEMS, requested_discount=0.25)
    assert q["subtotal"] == 2700.00      # 2400 + 300
    assert q["discount_total"] == 120.00  # 5% volume tier on the 3 pages
    assert q["total"] == 2580.00
    assert codes(q) == ["discount_above_cap"]


def test_request_within_cap_is_applied():
    q = build_quote(ITEMS, requested_discount=0.10)
    # 10% beats the 5% tier on pages (240) and applies to maintenance too (30)
    assert q["discount_total"] == 270.00
    assert q["total"] == 2430.00
    assert q["flags"] == []


def test_owner_override_wins():
    q = build_quote(ITEMS, requested_discount=0.25, override_discount=0.20)
    assert q["total"] == 2160.00


def test_rush_surcharge():
    q = build_quote(ITEMS, deadline_days=3)
    assert q["surcharge"] == 516.00      # 20% of 2580
    assert q["total"] == 3096.00
    assert "rush_deadline" in codes(q)


def test_unknown_item_flagged():
    q = build_quote([{"catalog_id": "mobile-app", "quantity": 1}])
    assert "item_not_in_catalog" in codes(q)
    assert q["lines"] == []
    assert q["total"] == 0.0


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
