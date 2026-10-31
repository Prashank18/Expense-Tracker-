# Expense-Tracker-

## Expense Tracker

An account-based tracker for debits, credits, category budgets, and daily/monthly/yearly charts. It supports the existing Python + SQLite setup and an optional Supabase cloud backend.

## Run locally

```powershell
python server.py
```

Open `http://127.0.0.1:8000`, create an account, then add debit and credit entries. The default database is created at `data/ledger.sqlite3`. To use another path, set `EXPENSE_DB_PATH` before starting the server.

The Python + SQLite mode requires `python server.py`; opening `index.html` directly will not provide that API. Once Supabase is configured, the cloud mode can also run from static hosting such as GitHub Pages.

## Supabase cloud setup

1. Create a Supabase project and open its SQL Editor.
2. Run [`supabase/migrations/20261101000000_expense_tracker.sql`](supabase/migrations/20261101000000_expense_tracker.sql) to create the profile, transaction, budget, and alert-event tables, profile trigger, and row-level security policies.
3. In Project Settings, add the app's full URL (for local use, `http://127.0.0.1:8000/`; for GitHub Pages, include the repository path) to the Auth URL Configuration's allowed redirect URLs. Enable email/password sign-in and ensure the `public` schema is available through the Data API.
4. Put the project's URL and publishable key in `supabase-config.js`. The publishable key is designed for browser use; never put a Supabase secret/service-role key in this file.

```js
window.EXPENSE_TRACKER_SUPABASE = {
  url: "https://YOUR_PROJECT.supabase.co",
  publishableKey: "YOUR_PUBLISHABLE_KEY",
  whatsappAlertsEnabled: false,
};
```

5. Run `python server.py` locally or publish the app files to a static host. New sign-ups may need email confirmation, depending on the project's Auth settings. Existing local SQLite accounts are not automatically copied to Supabase.

The app uses Supabase Auth and sends database requests as the signed-in user. Row-level security ties each profile, transaction, and budget to `auth.uid()`. SQLite mode continues to work when the URL and key are blank.

### Supabase WhatsApp alerts

Cloud-mode WhatsApp delivery is optional. Link the Supabase CLI to your project and deploy the included authenticated Edge Function:

```powershell
supabase login
supabase link --project-ref YOUR_PROJECT_REF
supabase functions deploy check-budget-alert
```

Then set these Edge Function secrets in the Supabase Dashboard:

```text
TWILIO_ACCOUNT_SID
TWILIO_AUTH_TOKEN
TWILIO_WHATSAPP_FROM
TWILIO_WHATSAPP_CONTENT_SID
```

The Twilio content template needs three variables in this order: category, current spending, and monthly limit. Once the function and secrets are configured, set `whatsappAlertsEnabled: true` in `supabase-config.js`. Users must also enter their international-format number and opt in under **Preferences**. Alert events are deduplicated by user, category, and month.

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
