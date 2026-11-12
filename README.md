# QuoteLoop

**Paste any customer request. QuoteLoop turns it into a policy-checked PayPal invoice and follows it until it is paid.**

Small businesses get requests scattered across email, chat and forms. Someone has to read each one, work out what to charge, apply the discount rules, retype it all into an invoice and chase the payment. QuoteLoop does that chain, and a person approves before anything is sent.

> AI reads. Rules decide. A human approves. PayPal closes the loop.

- **Live demo:** https://YOUR-SERVICE.onrender.com  (free hosting: the first load can take about a minute)
- **Demo video:** YOUR_YOUTUBE_LINK

## How it works

1. **Paste** a customer message (email, WhatsApp-style chat, form text).
2. **Read:** an LLM (Groq) extracts the customer, the items, quantities, the deadline and any discount the customer asked for. It never does arithmetic.
3. **Price:** plain code applies the business's own rules from `pricing.json`: catalog prices, volume tiers, a maximum automatic discount and a rush surcharge. Anything outside policy becomes a visible flag, for example "customer asked for 25%, policy allows 15%".
4. **Review:** the owner edits quantities, counters a discount request, and sees a step-by-step explanation of how every number was reached.
5. **Approve:** one click creates and sends a real invoice through the PayPal Invoicing API.
6. **Follow:** PayPal webhooks (with a polling fallback) update the status live: Drafted, Sent, Paid.
7. **Follow up:** when the customer replies ("could you do 20% off?"), the follow-up agent reads the reply, checks any discount request against the same pricing rules, and drafts an answer. The owner edits it and sends it as a PayPal invoice reminder.

## How it uses PayPal

All calls go to the PayPal sandbox (`app/paypal.py`):

| Purpose | API |
|---|---|
| Authentication | `POST /v1/oauth2/token` (client credentials, token cached) |
| Invoice number | `POST /v2/invoicing/generate-next-invoice-number` |
| Create the invoice | `POST /v2/invoicing/invoices` (items, per-line discounts, rush fee, payment terms, note) |
| Send it | `POST /v2/invoicing/invoices/{id}/send` |
| Check status | `GET /v2/invoicing/invoices/{id}` |
| Payment events | Webhooks for `INVOICING.INVOICE.*` |
| Webhook trust | `POST /v1/notifications/verify-webhook-signature` on every incoming event |
| Reminders | `POST /v2/invoicing/invoices/{id}/remind` (the follow-up agent's approved reply is sent as the reminder note) |

## How it uses AI

Groq (model set by `GROQ_MODEL`) in JSON mode reads the message and returns structured data. The result is validated, retried once if the JSON is bad, and low-confidence items are highlighted for the owner. Prices, discounts, surcharges and totals are computed by code, so the AI cannot invent a number.

## Try it (for judges)

1. Open the live demo.
2. Choose **Big order, big discount ask** and press **Build quote**.
3. Press **Counter at 15%** to answer the discount request, then **Approve and send**.
4. Press **Open invoice in PayPal** and pay as the sandbox buyer:
   - Email: `SANDBOX_BUYER_EMAIL`
   - Password: `SANDBOX_BUYER_PASSWORD`
5. Watch the Payment column and the Invoices table (AG Grid) turn to **Paid**.

To see the follow-up agent, stop after step 3 and, in the **Customer replied?** box under the Payment column, press **Use an example** and **Check reply**. Edit the draft and press **Send as PayPal reminder**.

Every invoice is sent to the sandbox buyer above, because sandbox emails are not delivered.

## Run it locally

```bash
python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env               # fill in your PayPal sandbox and Groq keys
uvicorn app.main:app --reload      # http://localhost:8000
```

Locally the app checks PayPal every few seconds, so payments show up without a public webhook URL.

### PayPal sandbox setup

1. At https://developer.paypal.com open **Testing Tools, Sandbox Accounts**. You need a **Business** account (the merchant) and a **Personal** account (the buyer). Create the buyer in a country PayPal can invoice, such as the United States.
2. Under **Apps & Credentials** (Sandbox) create an app linked to the Business account and copy the Client ID and Secret into `.env`.
3. Put the buyer's email in `FORCE_RECIPIENT_EMAIL`.

### Webhooks (needs a public URL)

Deploy to Render using `render.yaml`, then in the PayPal app add a webhook for `https://<your-service>.onrender.com/api/webhooks/paypal` with the `INVOICING.INVOICE.*` events and put its Webhook ID in `PAYPAL_WEBHOOK_ID`. Free Render instances use temporary storage, so the SQLite file resets when the service restarts. That is fine for a demo.

## Architecture

```
Pasted message --> /api/extract --> Groq (structured JSON)
                                      |
                  pricing engine (pricing.json, plain code) --> flags + step trace
                                      |
                     editable quote in the browser --> Approve
                                      |
        PayPal: invoice number --> create --> send
                                      |
 PayPal webhook (signature verified) / polling --> SQLite --> live timeline + AG Grid
```

Stack: Python, FastAPI, SQLite, vanilla JavaScript, AG Grid Community (invoice table with search, filters, live status flash and totals row).

## Pricing rules

Everything a business decides lives in `pricing.json`: catalog, volume tiers, the maximum automatic discount, rush surcharge and payment terms. The sample file describes a fictional web design studio.

## API

| Route | What it does |
|---|---|
| `POST /api/extract` | message text to extracted fields and a priced quote |
| `POST /api/quote` | re-price after the owner edits items or decides a discount |
| `POST /api/invoices/approve` | create and send the PayPal invoice |
| `GET /api/invoices` | invoices with status |
| `GET /api/invoices/{id}/events` | timeline for one invoice |
| `POST /api/invoices/{id}/followup` | follow-up agent: customer reply to intent, policy decision and a drafted answer |
| `POST /api/invoices/{id}/remind` | send the approved reply as a PayPal reminder |
| `POST /api/webhooks/paypal` | PayPal webhook receiver |

## Tests

```bash
pytest -q
```

Covers the pricing rules, extraction (with the LLM mocked) and the approve-to-paid flow (with PayPal mocked).

## License

MIT, see `LICENSE`.