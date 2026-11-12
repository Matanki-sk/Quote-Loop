"""QuoteLoop API: quote -> approve -> PayPal invoice -> webhook/polling -> status."""
import asyncio
import json
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import BackgroundTasks, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import config, db, followup, llm, paypal
from .pricing import build_quote

STATIC_DIR = Path(__file__).resolve().parent.parent / "static"

TERMINAL_STATUSES = {"paid", "marked_as_paid", "cancelled", "refunded", "marked_as_refunded"}

# PayPal webhook event name -> our status
WEBHOOK_STATUS = {
    "INVOICING.INVOICE.CREATED": "draft",
    "INVOICING.INVOICE.PAID": "paid",
    "INVOICING.INVOICE.CANCELLED": "cancelled",
    "INVOICING.INVOICE.REFUNDED": "refunded",
    "INVOICING.INVOICE.SCHEDULED": "scheduled",
}


@asynccontextmanager
async def lifespan(_: FastAPI):
    db.init_db()
    yield


app = FastAPI(title="QuoteLoop", lifespan=lifespan)


# ---------- models ----------
class QuoteItem(BaseModel):
    catalog_id: str
    quantity: int = Field(ge=1)


class QuoteRequest(BaseModel):
    items: list[QuoteItem]
    requested_discount: float | None = Field(default=None, ge=0, le=1)
    deadline_days: int | None = Field(default=None, ge=0)
    override_discount: float | None = Field(default=None, ge=0, le=1)


class ExtractRequest(BaseModel):
    text: str = Field(min_length=3, max_length=5000)


class ReplyRequest(BaseModel):
    text: str = Field(min_length=3, max_length=3000)


class RemindRequest(BaseModel):
    note: str = Field(min_length=3, max_length=3000)


class ApproveRequest(BaseModel):
    customer_name: str
    customer_email: str
    quote: dict
    note: str = ""


# ---------- status handling ----------
def apply_status(paypal_id: str, new_status: str, source: str) -> None:
    """Update status once per change and log it on the timeline."""
    new_status = (new_status or "").lower()
    if not new_status:
        return
    if db.get_status(paypal_id) == new_status:
        return
    db.set_status(paypal_id, new_status)
    db.add_event(paypal_id, new_status, f"via {source}")


async def poll_invoice(paypal_id: str) -> None:
    """Fallback for slow/missed webhooks: check PayPal every few seconds."""
    for _ in range(config.POLL_MAX_TRIES):
        await asyncio.sleep(config.POLL_INTERVAL_SECONDS)
        try:
            inv = await paypal.get_invoice(paypal_id)
        except Exception:
            continue
        status = (inv.get("status") or "").lower()
        apply_status(paypal_id, status, "polling")
        if status in TERMINAL_STATUSES:
            return


# ---------- routes ----------
@app.get("/health")
def health():
    return {"ok": True}


@app.post("/api/quote")
def quote(req: QuoteRequest):
    return build_quote(
        [i.model_dump() for i in req.items],
        requested_discount=req.requested_discount,
        deadline_days=req.deadline_days,
        override_discount=req.override_discount,
    )


@app.post("/api/extract")
async def extract(req: ExtractRequest):
    """Pasted customer message -> extracted fields -> priced quote with flags and a step trace."""
    try:
        extracted = await llm.extract_request(req.text)
    except llm.ExtractionError as e:
        raise HTTPException(e.status, str(e))

    q = build_quote(
        [{"catalog_id": i["catalog_id"], "quantity": i["quantity"]} for i in extracted["items"]],
        requested_discount=extracted["requested_discount"],
        deadline_days=extracted["deadline_days"],
    )

    trace = [f"Read the message: found {len(extracted['items'])} item(s)"]
    if extracted["summary"]:
        trace.append(f"Summary: {extracted['summary']}")
    if extracted["deadline_days"] is not None:
        trace.append(f"Deadline: {extracted['deadline_days']} day(s) from today")
    q["trace"] = trace + q["trace"]

    if not extracted["items"]:
        q["flags"].append({"code": "no_items_found", "severity": "error",
                           "message": "No catalog items could be found in this message."})
    for it in extracted["items"]:
        if it["confidence"] == "low":
            q["flags"].append({
                "code": "low_confidence", "severity": "warning",
                "message": f"Not sure about '{it['requested_name'] or it['catalog_id']}' "
                           f"(quantity {it['quantity']}). Please check it.",
            })
    return {"extracted": extracted, "quote": q}


@app.post("/api/invoices/approve")
async def approve(req: ApproveRequest, background: BackgroundTasks):
    q = req.quote
    if not q.get("lines"):
        raise HTTPException(400, "Quote has no line items")
    if any(f.get("severity") == "error" for f in q.get("flags", [])):
        raise HTTPException(400, "Resolve the error flags before approving")

    recipient = config.FORCE_RECIPIENT_EMAIL or req.customer_email
    try:
        number = await paypal.generate_invoice_number()
        payload = paypal.build_invoice_payload(
            invoice_number=number,
            customer_name=req.customer_name,
            customer_email=recipient,
            lines=q["lines"],
            currency=q.get("currency", "USD"),
            note=req.note,
            payment_term=q.get("payment_term", "NET_14"),
        )
        paypal_id = await paypal.create_invoice(payload)
        await paypal.send_invoice(paypal_id)
        inv = await paypal.get_invoice(paypal_id)
    except paypal.PayPalError as e:
        raise HTTPException(502, str(e))

    pay_url = inv.get("detail", {}).get("metadata", {}).get("recipient_view_url", "")
    status = (inv.get("status") or "sent").lower()

    db.insert_invoice(
        paypal_invoice_id=paypal_id, invoice_number=number,
        customer_name=req.customer_name, customer_email=recipient,
        currency=q.get("currency", "USD"), total=q["total"],
        items=q["lines"], flags=q.get("flags", []), pay_url=pay_url, status="draft",
    )
    db.add_event(paypal_id, "draft", "invoice created in PayPal")
    apply_status(paypal_id, status, "send")
    background.add_task(poll_invoice, paypal_id)
    return {"paypal_invoice_id": paypal_id, "invoice_number": number,
            "status": status, "pay_url": pay_url}


@app.get("/api/invoices")
def invoices():
    return db.list_invoices()


@app.get("/api/invoices/{paypal_id}/events")
def invoice_events(paypal_id: str):
    return db.list_events(paypal_id)


@app.post("/api/invoices/{paypal_id}/followup")
async def invoice_followup(paypal_id: str, req: ReplyRequest):
    """Follow-up agent: customer's reply -> intent, policy decision and a drafted answer."""
    inv = db.get_invoice(paypal_id)
    if inv is None:
        raise HTTPException(404, "Invoice not found")
    try:
        return await followup.handle_reply(inv, req.text)
    except llm.ExtractionError as e:
        raise HTTPException(e.status, str(e))


@app.post("/api/invoices/{paypal_id}/remind")
async def invoice_remind(paypal_id: str, req: RemindRequest):
    """Send the approved reply to the customer as a PayPal invoice reminder."""
    inv = db.get_invoice(paypal_id)
    if inv is None:
        raise HTTPException(404, "Invoice not found")
    if inv["status"] in followup.CLOSED_STATUSES:
        raise HTTPException(400, "This invoice is already closed, so a reminder cannot be sent.")
    try:
        await paypal.remind_invoice(paypal_id, f"Re: invoice #{inv['invoice_number']}", req.note)
    except paypal.PayPalError as e:
        raise HTTPException(502, str(e))
    db.add_event(paypal_id, "reminder", "reminder sent through PayPal")
    return {"ok": True}


@app.post("/api/webhooks/paypal")
async def paypal_webhook(request: Request):
    raw = await request.body()
    try:
        event = json.loads(raw)
    except ValueError:
        raise HTTPException(400, "Invalid JSON")

    if not config.SKIP_WEBHOOK_VERIFY:
        try:
            ok = await paypal.verify_webhook(request.headers, event)
        except paypal.PayPalError as e:
            raise HTTPException(502, str(e))
        if not ok:
            raise HTTPException(400, "Webhook signature verification failed")

    event_type = event.get("event_type", "")
    resource = event.get("resource", {}) or {}
    paypal_id = (resource.get("invoice") or {}).get("id") or resource.get("id")
    status = WEBHOOK_STATUS.get(event_type)
    if paypal_id and status and db.get_status(paypal_id) is not None:
        apply_status(paypal_id, status, "webhook")
    return {"received": True}


if STATIC_DIR.exists():
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

    @app.get("/")
    def index():
        return FileResponse(STATIC_DIR / "index.html")