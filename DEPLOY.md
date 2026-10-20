# PlumberFlow — Deployment Guide

FastAPI bot on Fly.io. Data lives in Neon (Postgres). WhatsApp Cloud API talks to customers and
plumbers; Telegram is where leads reach the founder and where the founder routes them.

```
main.py              FastAPI app: webhooks, customer flow, plumber commands, sweeps
services.py          Builds the stores, clients and services from environment variables
app_store.py         Plumber registry and admin queries on top of the packages
dispatch/            Telegram buttons, WhatsApp offer, first "take" wins
verification/        Done / no go, customer checks, invoices, reminders
migrations/          001 job lifecycle, 002 dispatch, 003 self-pause, 004 pending approval, 005 lead credits
tests/               pytest suite
WHATSAPP_TEMPLATES.md   Templates to submit to Meta
```

## How a job moves

1. A homeowner messages the WhatsApp number and answers the lead questions.
2. The lead is posted to the founder's Telegram with buttons: offer to a plumber, offer to the
   matching plumbers, pick one, or skip.
3. The plumber gets a WhatsApp offer (suburb and problem only) and replies `take`. The first
   reply wins and receives the customer's details.
4. The plumber sends `done` and the amount, or `no go`. The customer is asked to confirm 2 hours
   later (buttons), or after 24 hours if the plumber stays silent.
5. Friday from 17:00 SAST each plumber gets that week's invoice (R220 per charged job). You mark
   it paid in Telegram. Overdue invoices get a reminder, then a lead pause.

Every 10 minutes an in-process scheduler runs the timers. That is why the app must run on
**one machine only**.

## One-time setup

### 1. Neon

Migrations are applied with the **direct** connection string (host without `-pooler`):

```bash
psql "$NEON_DIRECT_URL" -f migrations/001_job_lifecycle.sql
psql "$NEON_DIRECT_URL" -f migrations/002_dispatch.sql
psql "$NEON_DIRECT_URL" -f migrations/004_pending_approval.sql
psql "$NEON_DIRECT_URL" -f migrations/005_credits.sql
```

001 and 002 are already applied on the PlumberFlow project. 004 **must be applied before you deploy
the version that holds new plumbers for approval**. It supersedes 003 (it allows the same
`self_paused` reason as well as `pending_approval`), so 003 can be skipped. Without 004, new plumbers
are saved but not held, and the Telegram message says "APPROVAL HOLD FAILED". It also turns on
PAUSE / RESUME, which otherwise tell the plumber to try again later. Safe to re-run.

Trial leads: `plumbers.trial_stage` is how many free jobs a plumber has left. `/approve` sets it to
`TRIAL_LEADS` only if it is empty (NULL), so nobody's count is ever reset. A job taken while the
count is above 0 has a fee of R0, is flagged `trial_lead`, and uses one. The offer says
"Trial lead · no fee" for that plumber, and only the plumber who takes the lead uses one. Free jobs
are never invoiced and do not count towards the "three paid jobs" credit offer. To give someone
more, `UPDATE plumbers SET trial_stage = 3 WHERE id = '...'`. To take them away, set it to 0.

Lead credits: once the trial leads are used, a lead costs one credit. **Apply 005 before deploying
this version** (it adds `plumbers.credits` and the `credit_orders` table; the old bot ignores both).
Packs are 2 credits (R440), 5 credits (R1,045, 5% off) and 10 credits (R1,980, 10% off); one credit is
R220. A plumber sends *BUY*, picks a pack, and gets your PayShap number (`PAYSHAP_NUMBER`) with an
order reference. You get the order in Telegram; when the PayShap payment arrives, `/credit C-0001` adds
the credits and WhatsApps them. `/orders` lists unpaid orders, `/credit C-0001 cancel` drops one,
`/grant <id> <n>` adds credits by hand. A trial lead is used before a credit. A plumber with neither
is not suggested for leads and cannot take one until they buy. A job that ends with no charge (no go,
or a dispute settled as no charge) returns its credit. Credits never expire. Weekly invoices are no
longer created for new jobs; invoices already sent stay valid and are chased as before.

New plumbers wait for the founder. `/pending` in Telegram lists them, `/approve <id>` lets one start
getting leads and WhatsApps them. Plumbers already in the table are not affected.

The bot runs on the **pooled** string (host contains `-pooler`), set as `DATABASE_URL`.

### 2. Load the plumber roster

Plumbers are rows in `plumbers`. Offers are matched on `areas`, so keep it filled in (lowercase
suburbs). `whatsapp` is digits only, starting with `27`.

```sql
INSERT INTO plumbers (first_name, whatsapp, areas, services, experience_years, pirb_registered, availability)
VALUES ('Sipho', '27821234567', ARRAY['randburg','fourways','northcliff'],
        ARRAY['Burst pipe','Leak'], 8, false, 'Mon–Fri, 8am–5pm');
```

Plumbers can also register themselves by sending `JOIN` to the WhatsApp number. They become
active immediately and appear in your "Pick a plumber" list.

### 3. Fly.io

Install `flyctl` and log in:

```powershell
powershell -Command "iwr https://fly.io/install.ps1 -useb | iex"
fly auth login
```

`fly.toml` and the `Dockerfile` sit at the repo root next to `main.py`. If Fly's GitHub
integration is used, the working directory must be that folder.

### 4. Secrets

```bash
fly secrets set DATABASE_URL="<pooled-url>" WA_TOKEN="<token>" WA_PHONE_NUMBER_ID="<id>" VERIFY_TOKEN="<random>" TELEGRAM_TOKEN="<token>" FOUNDER_CHAT_ID="<chat-id>" FOUNDER_USER_ID="<user-id>" TELEGRAM_WEBHOOK_SECRET="<random>" ANTHROPIC_API_KEY="<key>" ADMIN_PHONE="<27…>"
```

| Variable | What it is |
|---|---|
| `DATABASE_URL` | Neon pooled connection string |
| `WA_TOKEN`, `WA_PHONE_NUMBER_ID` | WhatsApp Cloud API token and phone number id |
| `VERIFY_TOKEN` | Must match the verify token you enter in Meta's webhook settings |
| `TELEGRAM_TOKEN` | Bot token from @BotFather |
| `FOUNDER_CHAT_ID` | Chat where leads and alerts arrive |
| `FOUNDER_USER_ID` | Your Telegram user id. Only this user can press buttons and run commands |
| `TELEGRAM_WEBHOOK_SECRET` | Random string; Telegram sends it back on every call (step 6) |
| `ANTHROPIC_API_KEY` | Claude Haiku, for understanding messages |
| `ADMIN_PHONE` | Optional. Your WhatsApp number, for STATS, PLUMBERS and BROADCAST |
| `PAYSHAP_NUMBER` | Needed to sell credits by PayShap: the cellphone number plumbers pay, e.g. `0821234567`. Shown with their order and reference |
| `PAYSHAP_NAME` | Optional. The name that shows when they pay that number, so they know it is right |
| `CREDIT_PAYMENT_DETAILS` | Optional. Used only when `PAYSHAP_NUMBER` is not set. Your bank details as text; use `\|` for a new line, e.g. `FNB \| Princora \| Acc 62... \| Branch 250655`. Sent to a plumber with their order |
| `TRIAL_LEADS` | Optional, default `5`. Trial leads given when you `/approve` a plumber who has never had any |
| `WA_FALLBACK_TEMPLATE` | Optional. Approved template name used when WhatsApp refuses free text (see `WHATSAPP_TEMPLATES.md`) |

The older names `WHATSAPP_TOKEN`, `PHONE_NUMBER_ID`, `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID`
are still read, so secrets already on Fly keep working. If Supabase secrets are still set,
remove them: `fly secrets unset SUPABASE_URL SUPABASE_KEY`.

### 5. Deploy

Reduce to one machine first. Two would run every timer twice, and a stopped second machine can
start itself when traffic arrives:

```bash
fly scale count 1 -a plumberflow
fly deploy --ha=false -a plumberflow
curl https://plumberflow.fly.dev/health
```

**A bad deploy cannot replace a working bot.** `fly.toml` uses a blue-green deploy with a health
check on `/health`: Fly starts the new version beside the old one and only switches over once
`/health` returns 200. `/health` is 200 only when the bot started, its scheduler is running and the
database answers. Otherwise it returns 503 (`"status": "degraded"`), the deploy fails, and the
version that was running keeps serving. Healthy looks like:

```json
{"status":"ok","service":"plumberflow","services":"ready","database":"ok","scheduler":"running","admin":"configured"}
```

**If the bot will not start, read the end of `fly logs`.** Before it touches anything it checks every
secret and prints one report naming each wrong one, with no values:

```
PLUMBERFLOW CANNOT START. Fix these settings, then deploy again:

  - DATABASE_URL looks like the whole psql command was pasted. ...
  - FOUNDER_USER_ID must be digits only, your Telegram user id, ...
```

The mistakes that have actually happened: `DATABASE_URL` must be only the connection string
(`postgresql://...`, the pooled one) and not the `psql '...'` command Neon also shows; and
`FOUNDER_USER_ID` must be only digits. Put no `<` `>`, no quotes and no `--stage` inside a value.
`--stage` goes after the values, outside any quotes, and saves the secrets without restarting the
bot that is running:

```bash
fly secrets set -a plumberflow FOUNDER_USER_ID=123456789 DATABASE_URL='postgresql://...' --stage
```

To go back to a previous version without building anything:
`fly releases -a plumberflow`, then `fly deploy -a plumberflow --image registry.fly.io/plumberflow:<image of that release>`.

### 6. Telegram webhook

Run once, with the same secret you set above:

```bash
curl "https://api.telegram.org/bot<TOKEN>/setWebhook" -d url=https://plumberflow.fly.dev/telegram -d secret_token=<TELEGRAM_WEBHOOK_SECRET> -d 'allowed_updates=["message","callback_query"]'
```

### 7. Meta webhook

1. developers.facebook.com → your app → **WhatsApp** → **Configuration** → **Webhook** → **Edit**
2. **Callback URL:** `https://plumberflow.fly.dev/webhook`
3. **Verify token:** the value of `VERIFY_TOKEN`
4. Subscribe to the **messages** field.

### 8. Templates

Free text only works within 24 hours of the person's last message. Submit the templates in
`WHATSAPP_TEMPLATES.md`, then tell the developer the approved names so they can be wired in.
Until then, messages to plumbers and customers who have been quiet for a day will fail and
show up in `fly logs`.

## Day to day

Founder commands, in the bot's Telegram chat:

| Command | What it does |
|---|---|
| `/unpaid` | Invoices waiting for payment, with a short id |
| `/paid <id>` | Mark an invoice paid; restores leads if it was the reason for a pause |
| `/disputes` | Jobs that need your decision |
| `/resolve <id> charge\|no_charge [note]` | Settle a dispute |

Plumbers: `take`, `done R2400`, `no go`, `STATUS`, `PAUSE`, `RESUME`, `AREAS`, `SERVICES`, `HELP`.
Admin (WhatsApp, from `ADMIN_PHONE`): `STATS`, `PLUMBERS`, `BROADCAST <message>`.

| Command | What it does |
|---|---|
| `fly logs` | Stream live logs |
| `fly status` | Check the machine is running |
| `fly secrets list` | Which secrets are set (not their values) |
| `fly deploy --ha=false` | Ship new code |

## Local development and tests

```bash
pip install -r requirements-dev.txt
pytest
```

Tests that need Postgres are skipped unless `PLUMBERFLOW_TEST_DSN` points at a database you can
write to, ideally a throwaway Neon branch (never `main`). They only create and drop a schema
called `pf_test`.

To run the app locally, copy `.env.example` to `.env`, fill it in, export the values, and run
`uvicorn main:app --reload --port 8080`. Use ngrok if Meta or Telegram must reach it, and point
the webhooks back at Fly afterwards.
