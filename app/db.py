"""Tiny SQLite layer. Two tables: invoices and events (the timeline)."""
import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone

from . import config


@contextmanager
def conn():
    c = sqlite3.connect(config.DATABASE_PATH)
    c.row_factory = sqlite3.Row
    try:
        yield c
        c.commit()
    finally:
        c.close()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def init_db() -> None:
    with conn() as c:
        c.executescript(
            """
            CREATE TABLE IF NOT EXISTS invoices (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                paypal_invoice_id TEXT UNIQUE,
                invoice_number TEXT,
                customer_name TEXT,
                customer_email TEXT,
                currency TEXT,
                total REAL,
                items_json TEXT,
                flags_json TEXT,
                pay_url TEXT,
                status TEXT,
                created_at TEXT,
                paid_at TEXT
            );
            CREATE TABLE IF NOT EXISTS events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                paypal_invoice_id TEXT,
                type TEXT,
                detail TEXT,
                created_at TEXT
            );
            """
        )


def insert_invoice(*, paypal_invoice_id, invoice_number, customer_name, customer_email,
                   currency, total, items, flags, pay_url, status) -> None:
    with conn() as c:
        c.execute(
            """INSERT INTO invoices (paypal_invoice_id, invoice_number, customer_name,
               customer_email, currency, total, items_json, flags_json, pay_url, status, created_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            (paypal_invoice_id, invoice_number, customer_name, customer_email, currency, total,
             json.dumps(items), json.dumps(flags), pay_url, status, _now()),
        )


def add_event(paypal_invoice_id: str, type_: str, detail: str = "") -> None:
    with conn() as c:
        c.execute(
            "INSERT INTO events (paypal_invoice_id, type, detail, created_at) VALUES (?,?,?,?)",
            (paypal_invoice_id, type_, detail, _now()),
        )


def get_status(paypal_invoice_id: str) -> str | None:
    with conn() as c:
        row = c.execute("SELECT status FROM invoices WHERE paypal_invoice_id=?",
                        (paypal_invoice_id,)).fetchone()
    return row["status"] if row else None


def set_status(paypal_invoice_id: str, status: str) -> None:
    with conn() as c:
        if status in ("paid", "marked_as_paid"):
            c.execute("UPDATE invoices SET status=?, paid_at=? WHERE paypal_invoice_id=?",
                      (status, _now(), paypal_invoice_id))
        else:
            c.execute("UPDATE invoices SET status=? WHERE paypal_invoice_id=?",
                      (status, paypal_invoice_id))


def list_invoices() -> list[dict]:
    with conn() as c:
        rows = c.execute("SELECT * FROM invoices ORDER BY id DESC").fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["items"] = json.loads(d.pop("items_json") or "[]")
        d["flags"] = json.loads(d.pop("flags_json") or "[]")
        out.append(d)
    return out


def list_events(paypal_invoice_id: str) -> list[dict]:
    with conn() as c:
        rows = c.execute("SELECT type, detail, created_at FROM events "
                         "WHERE paypal_invoice_id=? ORDER BY id", (paypal_invoice_id,)).fetchall()
    return [dict(r) for r in rows]


def get_invoice(paypal_invoice_id: str) -> dict | None:
    for inv in list_invoices():
        if inv["paypal_invoice_id"] == paypal_invoice_id:
            return inv
    return None