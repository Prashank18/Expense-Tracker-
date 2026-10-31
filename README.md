# Expense-Tracker-

## Ledger House

A small, account-based expense tracker. It runs on Python's standard library and stores account data in SQLite.

## Run locally

```powershell
python server.py
```

Open `http://127.0.0.1:8000`, create an account, then add debit and credit entries. The default database is created at `data/ledger.sqlite3`. To use another path, set `EXPENSE_DB_PATH` before starting the server.

Run the automated checks with:

```powershell
python -m unittest -v
node --check app.js
```

## WhatsApp budget alerts

The app checks a category's current-month debit total when a debit or budget is saved. It sends at most one alert per budget category and month. The user must enter an international-format phone number and explicitly opt in under **Preferences**.

Delivery is optional and requires a Twilio WhatsApp sender and an approved WhatsApp content template. Set these server-side environment variables before starting the app:

```text
TWILIO_ACCOUNT_SID
TWILIO_AUTH_TOKEN
TWILIO_WHATSAPP_FROM
TWILIO_WHATSAPP_CONTENT_SID
```

The approved template must include three variables in this order: `{{1}}` for category, `{{2}}` for current spending, and `{{3}}` for the monthly limit. For example: “Budget update: {{1}} spending is {{2}} against your monthly limit of {{3}}.” The app submits the template SID and variable values through Twilio's Messages API. Without the provider setup, budgets still work and no message is sent.

## Deployment note

The built-in server is intended for local development. For deployment, run it behind a production-grade HTTPS reverse proxy, set `COOKIE_SECURE=1`, and restrict account registration if the service should be private.
