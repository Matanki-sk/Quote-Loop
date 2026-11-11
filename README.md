# QuoteLoop

Turns a customer request (email, chat message, form) into a policy-checked **PayPal invoice**
and tracks it until it is paid. AI proposes, rules decide, a human approves.

Status: starter. Done so far: pricing engine, PayPal Invoicing client, webhook + polling fallback, SQLite, API.
Next: LLM extraction, the three-panel UI, AG Grid table, follow-up agent.

## 1. Get PayPal sandbox credentials (about 15 minutes)

1. Log in at https://developer.paypal.com (a normal PayPal account works).
2. Open **Testing Tools -> Sandbox Accounts**. Two accounts usually exist already:
   a **Business** one (the merchant) and a **Personal** one (the customer). Create them if missing.
3. Note the **email of the Personal account**. This is `FORCE_RECIPIENT_EMAIL`.
   (Open its "..." menu -> *View/Edit account* to see the sandbox password. You need it to pay.)
4. Open **Apps & Credentials**, switch to **Sandbox**, create an app (or use "Default Application"),
   and make sure it is linked to your Business sandbox account.
5. Copy the **Client ID** and **Secret**.

## 2. Run locally

```bash
python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env               # then fill in the values from step 1
python tests/test_pricing.py       # pricing rules sanity check
python scripts/send_test_invoice.py
```

The last command prints a **Pay link**. Open it in a private window, log in as the sandbox
Personal (buyer) account and pay. That is your first checkpoint.

Start the API:

```bash
uvicorn app.main:app --reload
```

Open http://localhost:8000 and http://localhost:8000/docs (interactive API docs).

## 3. Webhooks (needs a public URL, so deploy first)

1. Push the repo to GitHub (public, with the LICENSE file).
2. On https://render.com create a **Web Service** from the repo (it reads `render.yaml`).
   Add the environment variables when asked.
3. In the PayPal developer dashboard open your app, scroll to **Webhooks -> Add Webhook**:
   - URL: `https://<your-service>.onrender.com/api/webhooks/paypal`
   - Events: tick all of `INVOICING.INVOICE.*` (at least CREATED, PAID, CANCELLED, REFUNDED)
4. Copy the **Webhook ID** into the `PAYPAL_WEBHOOK_ID` variable on Render and redeploy.
5. Pay an invoice as the buyer. The status should flip to `paid` within seconds, either via the
   webhook or, at worst, via the polling fallback.

Note: Render's free tier has a temporary disk, so the SQLite file resets on redeploy. That is fine for a demo.

## API

| Route | What it does |
|---|---|
| `POST /api/quote` | items + optional requested discount / deadline -> priced quote with flags and a step trace |
| `POST /api/invoices/approve` | creates and sends the PayPal invoice for an approved quote |
| `GET /api/invoices` | all invoices with status |
| `GET /api/invoices/{id}/events` | timeline for one invoice |
| `POST /api/webhooks/paypal` | PayPal webhook receiver (signature verified) |

## Pricing rules

All business rules live in `pricing.json` (catalog, volume tiers, 15% max auto discount, rush surcharge, payment terms).
The LLM never does money math; `app/pricing.py` does.

## Tests

```bash
pytest -q
```

## License

MIT
