import asyncio
import hmac
import json
import logging
import os
import re
from collections import deque
from contextlib import asynccontextmanager

from apscheduler.schedulers.background import BackgroundScheduler
from fastapi import FastAPI, Request, Response

from dispatch.phone import normalize_phone
from dispatch.router import handle_whatsapp_webhook
from dispatch.telegram import handle_telegram_update
from services import Services, build_services

# ---------------------------------------------------------------------------
# Config – pulled from environment variables (set via `flyctl secrets`)
# Everything the dispatch and verification services need is read in services.py.
# ---------------------------------------------------------------------------
VERIFY_TOKEN            = os.environ.get("VERIFY_TOKEN", "plumberflow_verify_123")
TELEGRAM_WEBHOOK_SECRET = os.environ.get("TELEGRAM_WEBHOOK_SECRET", "")
ADMIN_PHONE             = os.environ.get("ADMIN_PHONE", "")     # your WhatsApp number (no +)
ANTHROPIC_API_KEY       = os.environ.get("ANTHROPIC_API_KEY")   # for the Haiku 4.5 NLU layer

logger = logging.getLogger("plumberflow")
logging.basicConfig(level=logging.INFO)

# ---------------------------------------------------------------------------
# Anthropic client (Claude Haiku 4.5 — NLU layer)
# ---------------------------------------------------------------------------
anthropic_client = None
if ANTHROPIC_API_KEY:
    try:
        from anthropic import AsyncAnthropic
        anthropic_client = AsyncAnthropic(api_key=ANTHROPIC_API_KEY)
        logger.info("Anthropic NLU layer enabled (claude-haiku-4-5)")
    except Exception as e:
        logger.warning(f"Anthropic init failed: {e}")

# ---------------------------------------------------------------------------
# Services (store, Telegram, WhatsApp, verification, dispatch) and sweeps
# ---------------------------------------------------------------------------
S: Services | None = None
_loop: asyncio.AbstractEventLoop | None = None
_scheduler: BackgroundScheduler | None = None

SWEEP_MINUTES = 10


def run_sweeps() -> None:
    """Each sweep is isolated so one failing send (for example a template Meta has not
    approved yet) cannot stop the others."""
    for name, fn in (
        ("verification.sweep_jobs", S.ver.sweep_jobs),
        ("verification.sweep_patterns", S.ver.sweep_patterns),
        ("verification.sweep_invoices", S.ver.sweep_invoices),
        ("dispatch.run_sweeps", S.disp.run_sweeps),
    ):
        try:
            fn()
        except Exception:
            logger.exception(f"{name} failed")


@asynccontextmanager
async def lifespan(app: FastAPI):
    global S, _loop, _scheduler
    _loop = asyncio.get_running_loop()
    if S is None:
        S = build_services()
    if not TELEGRAM_WEBHOOK_SECRET:
        logger.warning("TELEGRAM_WEBHOOK_SECRET is not set: /telegram accepts any caller")
    _scheduler = BackgroundScheduler(timezone="UTC")
    _scheduler.add_job(run_sweeps, "interval", minutes=SWEEP_MINUTES,
                       id="sweeps", max_instances=1, coalesce=True)
    _scheduler.start()
    yield
    _scheduler.shutdown(wait=False)
    S.store.close()


app = FastAPI(lifespan=lifespan)


async def db(fn, *args, **kwargs):
    """The stores are synchronous; keep them off the event loop."""
    return await asyncio.to_thread(fn, *args, **kwargs)


# ---------------------------------------------------------------------------
# In-memory conversation state (a restart drops half-finished conversations only)
# ---------------------------------------------------------------------------
conversations:         dict[str, dict] = {}   # customer WhatsApp flows
plumber_onboarding:    dict[str, dict] = {}   # plumber onboarding flows
plumber_command_state: dict[str, dict] = {}   # multi-step plumber commands (AREAS, SERVICES)
profile_names:         dict[str, str] = {}    # WhatsApp number → first name from the sender's profile
_seen_message_ids:     deque = deque(maxlen=1000)

JOB_TYPES = {
    "1": "Burst pipe",
    "2": "Blocked drain",
    "3": "Geyser problem",
    "4": "Leak",
    "5": "Other",
}

URGENCY_LEVELS = {
    "1": "Emergency — right now",
    "2": "Today",
    "3": "This week",
    "4": "Just a quote",
}

AVAILABILITY_OPTIONS = {
    "1": "Mon–Fri, 8am–5pm",
    "2": "Mon–Sat, 8am–6pm",
    "3": "24/7 including emergencies",
    "4": "Weekends only",
}

JOIN_KEYWORDS = {"join", "register", "sign up", "signup"}
PLUMBER_KEYWORDS = {"plumber", "i need a plumber", "need a plumber", "find plumber", "find a plumber"}

HOLD_LABELS = {
    "unresolved_jobs": "jobs waiting to be closed off",
    "unpaid_invoice":  "an unpaid invoice",
    "self_paused":     "you paused",
}

# ---------------------------------------------------------------------------
# Urgency tiers — drives ETA windows and self-help copy (Consumer Msg Library)
# ---------------------------------------------------------------------------
URGENCY_TIERS = {
    "Emergency — right now": {
        "tier":      "Critical",
        "eta":       "15–30 minutes",
        "self_help": (
            "While we find your plumber: if water is flooding, turn off your main "
            "stop-valve now — it's usually near the meter or boundary wall."
        ),
    },
    "Today": {
        "tier":      "Urgent",
        "eta":       "30–60 minutes",
        "self_help": None,
    },
    "This week": {
        "tier":      "Standard",
        "eta":       "1–3 hours",
        "self_help": None,
    },
    "Just a quote": {
        "tier":      "Planned",
        "eta":       "within 24 hours",
        "self_help": None,
    },
}

def get_tier_info(urgency: str) -> dict:
    return URGENCY_TIERS.get(urgency, {"tier": "Standard", "eta": "1–3 hours", "self_help": None})

def job_short_name(job_type: str) -> str:
    return {
        "Burst pipe":     "burst pipe",
        "Blocked drain":  "blocked drain",
        "Geyser problem": "geyser problem",
        "Leak":           "leak",
        "Other":          "plumbing issue",
    }.get(job_type, "plumbing issue")

# ---------------------------------------------------------------------------
# Quick keyword parsers for in-flow stage inputs (avoids an extra Haiku call)
# ---------------------------------------------------------------------------
def parse_job_type_text(text: str) -> str | None:
    t = text.lower()
    if any(w in t for w in ["burst", "flood", "pipe burst", "water everywhere"]):
        return "Burst pipe"
    if any(w in t for w in ["drain", "block", "clog", "toilet", "sewage", "backup"]):
        return "Blocked drain"
    if any(w in t for w in ["geyser", "hot water", "no hot", "heater", "boiler"]):
        return "Geyser problem"
    if any(w in t for w in ["leak", "drip", "dripping", "leaking", "seep"]):
        return "Leak"
    if any(w in t for w in ["other", "general", "misc"]):
        return "Other"
    return None

def parse_urgency_text(text: str) -> str | None:
    t = text.lower()
    if any(w in t for w in ["now", "asap", "emergency", "right now", "immediately", "urgent", "burst"]):
        return "Emergency — right now"
    if any(w in t for w in ["today", "this afternoon", "this morning", "few hours", "tonight"]):
        return "Today"
    if any(w in t for w in ["week", "no rush", "whenever", "not urgent", "few days"]):
        return "This week"
    if any(w in t for w in ["quote", "price", "cost", "estimate", "how much"]):
        return "Just a quote"
    return None

_YES_WORDS = {"yes", "yebo", "ya", "yep", "yeah", "sorted", "done", "all good", "all sorted", "fixed"}
_NO_WORDS  = {"no", "nope", "not yet", "nah"}
_NEVER_CAME = ("never came", "didn't come", "did not come", "no show", "nobody came",
               "no one came", "never showed", "didn't show", "did not show")

def quick_job_reply_label(text: str) -> str | None:
    """One-word answers to 'did he get it sorted?' need no model call."""
    t = re.sub(r"[^\w\s']", "", text.lower()).strip()
    if any(p in t for p in _NEVER_CAME):
        return "never_came"
    if t in _YES_WORDS:
        return "sorted"
    if t in _NO_WORDS:
        return "not_sorted"
    return None

# ---------------------------------------------------------------------------
# WhatsApp / Telegram senders
# ---------------------------------------------------------------------------
async def send(to: str, message: str):
    """Free-text reply to someone who has just messaged us (inside Meta's 24-hour window)."""
    try:
        await db(S.wa.send_text, to, message)
    except Exception as e:
        logger.error(f"WhatsApp send to {to} failed: {e}")

async def notify_founder(text: str):
    try:
        await db(S.tg.notify, text)
    except Exception as e:
        logger.error(f"Telegram notify failed: {e}")

def is_admin(phone: str) -> bool:
    return bool(ADMIN_PHONE) and normalize_phone(phone) == normalize_phone(ADMIN_PHONE)

async def get_plumber(phone: str) -> dict | None:
    return await db(S.store.find_plumber_by_whatsapp, phone)

# ---------------------------------------------------------------------------
# Webhook verification (GET)
# ---------------------------------------------------------------------------
@app.get("/webhook")
async def verify(request: Request):
    params    = request.query_params
    mode      = params.get("hub.mode")
    token     = params.get("hub.verify_token")
    challenge = params.get("hub.challenge")
    if mode == "subscribe" and token == VERIFY_TOKEN:
        logger.info("Webhook verified successfully")
        return Response(content=challenge, media_type="text/plain")
    return Response(content="Forbidden", status_code=403)

# ---------------------------------------------------------------------------
# Incoming WhatsApp messages (POST)
# ---------------------------------------------------------------------------
def _on_customer_text(phone: str, text: str) -> None:
    asyncio.run_coroutine_threadsafe(handle_text(phone, text.strip()), _loop).result()

def _on_plumber_unhandled(phone: str, text: str) -> None:
    asyncio.run_coroutine_threadsafe(handle_plumber_text(phone, text.strip()), _loop).result()

async def process_whatsapp_message(m: dict):
    sender, mtype = m.get("from", ""), m.get("type", "text")

    if mtype in ("text", "interactive"):
        # One message at a time keeps replies in the order the sender wrote them.
        payload = {"entry": [{"changes": [{"value": {"messages": [m]}}]}]}
        await asyncio.to_thread(
            handle_whatsapp_webhook, payload, S.disp, S.ver,
            on_customer_text=_on_customer_text, on_plumber_unhandled=_on_plumber_unhandled)
        return

    if mtype == "image":
        if await get_plumber(sender):
            await forward_plumber_to_founder(sender, "[sent a photo]")
        else:
            await handle_image(sender, m["image"].get("id", ""))
    elif mtype == "location":
        loc = m["location"]
        name = loc.get("name", f"{loc.get('latitude')}, {loc.get('longitude')}")
        if await get_plumber(sender):
            await forward_plumber_to_founder(sender, f"[sent a location] {name}")
        else:
            await handle_text(sender, name)

@app.post("/webhook")
async def receive(request: Request):
    body = await request.json()
    for e in body.get("entry", []):
        for change in e.get("changes", []):
            value = change.get("value", {})
            for c in value.get("contacts", []):
                name = ((c.get("profile") or {}).get("name") or "").split()
                if c.get("wa_id") and name:
                    if len(profile_names) > 5000:
                        profile_names.clear()
                    profile_names[c["wa_id"]] = name[0]
            for m in value.get("messages", []):
                mid = m.get("id")
                if mid in _seen_message_ids:
                    continue
                _seen_message_ids.append(mid)
                try:
                    await process_whatsapp_message(m)
                except Exception:
                    logger.exception(f"Failed handling WhatsApp message {mid}")
    return {"status": "ok"}

# ---------------------------------------------------------------------------
# Incoming Telegram updates (POST): button taps, and the founder's text commands
# ---------------------------------------------------------------------------
FOUNDER_HELP = (
    "Commands\n\n"
    "/unpaid — invoices waiting for payment\n"
    "/paid <id> — mark an invoice paid\n"
    "/disputes — jobs that need your decision\n"
    "/resolve <id> charge|no_charge [note] — settle a dispute\n\n"
    "<id> is the first few characters shown in the list."
)

def _rand(cents: int) -> str:
    return f"R{cents / 100:,.0f}" if cents % 100 == 0 else f"R{cents / 100:,.2f}"

async def handle_founder_command(update: dict):
    msg = update.get("message") or {}
    if (msg.get("from") or {}).get("id") != S.founder_user_id:
        return
    text = (msg.get("text") or "").strip()
    if not text.startswith("/"):
        return
    head, _, rest = text.partition(" ")
    cmd, args = head.split("@")[0].lower(), rest.split()

    if cmd in ("/start", "/help"):
        await notify_founder(FOUNDER_HELP)

    elif cmd == "/unpaid":
        rows = await db(S.store.list_unpaid_invoices)
        if not rows:
            await notify_founder("No unpaid invoices.")
            return
        lines = [f"{r['id'][:8]} · {r.get('first_name') or '?'} · {_rand(r['total_cents'])}" for r in rows]
        await notify_founder("Unpaid invoices\n\n" + "\n".join(lines))

    elif cmd == "/paid":
        inv = await db(S.store.find_invoice_by_prefix, args[0]) if args else None
        if not inv:
            await notify_founder("Usage: /paid <id>. No single invoice matches that id.")
            return
        await db(S.ver.mark_invoice_paid, inv["id"])
        await notify_founder(f"Marked paid: {inv['id'][:8]} · {_rand(inv['total_cents'])}")

    elif cmd == "/disputes":
        rows = await db(S.store.list_open_disputes)
        if not rows:
            await notify_founder("Nothing waiting on you.")
            return
        lines = [
            f"{r['id'][:8]} · {r['status']} · {r.get('first_name') or '?'} · "
            f"{' '.join(x for x in (r.get('suburb'), r.get('job_keyword')) if x) or 'job'}"
            + (f" · {', '.join(r['flags'])}" if r.get("flags") else "")
            for r in rows
        ]
        await notify_founder("Needs a decision\n\n" + "\n".join(lines))

    elif cmd == "/resolve":
        job = await db(S.store.find_job_by_prefix, args[0]) if args else None
        if not job or len(args) < 2 or args[1] not in ("charge", "no_charge"):
            await notify_founder("Usage: /resolve <id> charge|no_charge [note]")
            return
        try:
            await db(S.ver.admin_resolve, job["id"], args[1], " ".join(args[2:]))
        except ValueError as e:
            await notify_founder(str(e).capitalize() + ".")
            return
        await notify_founder(f"Resolved {job['id'][:8]}: {args[1].replace('_', ' ')}.")

    else:
        await notify_founder(FOUNDER_HELP)

@app.post("/telegram")
async def telegram_update(request: Request):
    if TELEGRAM_WEBHOOK_SECRET and not hmac.compare_digest(
        request.headers.get("X-Telegram-Bot-Api-Secret-Token", ""), TELEGRAM_WEBHOOK_SECRET
    ):
        return Response(content="Forbidden", status_code=403)
    update = await request.json()
    try:
        tapped = await db(handle_telegram_update, update, S.disp, S.tg,
                          founder_user_id=S.founder_user_id)
        if not tapped:
            await handle_founder_command(update)
    except Exception:
        logger.exception("Telegram update failed")
    return {"ok": True}

# ===========================================================================
# NLU LAYER — Claude Haiku 4.5 (understanding only; templates write the outbound text
# for jobs, the replies below are for the general customer chat)
# ===========================================================================
# Schema returned by the model. Kept tight on purpose — every extra field is
# more chances for the model to hallucinate.
NLU_SCHEMA = {
    "type": "object",
    "properties": {
        "intent": {
            "type": "string",
            "enum": [
                "request_plumber",   # customer wants a plumber
                "register_plumber",  # plumber wants to join the network
                "check_status",      # asking about their existing job
                "cancel",            # wants to cancel / restart
                "help",              # wants to know what the bot does
                "question",          # general question (pricing, hours, services)
                "greeting",          # hi / hello / good morning
                "chitchat",          # everything else
            ],
            "description": "The user's primary intent.",
        },
        "job_type": {
            "anyOf": [
                {"type": "string", "enum": ["Burst pipe", "Blocked drain", "Geyser problem", "Leak", "Other"]},
                {"type": "null"},
            ],
            "description": "If intent=request_plumber and the user mentioned the issue, map it to one of these. Otherwise null.",
        },
        "urgency": {
            "anyOf": [
                {"type": "string", "enum": ["Emergency — right now", "Today", "This week", "Just a quote"]},
                {"type": "null"},
            ],
            "description": "If intent=request_plumber and the user mentioned urgency, map it. Otherwise null.",
        },
        "location": {
            "anyOf": [
                {"type": "string"},
                {"type": "null"},
            ],
            "description": "Suburb or area name if mentioned (e.g. 'Randburg'). Otherwise null.",
        },
        "reply": {
            "type": "string",
            "description": (
                "A short, friendly WhatsApp reply (under 50 words). "
                "For request_plumber: confirm you'll find them a plumber and ask for any missing details (job_type, urgency, location). "
                "For register_plumber: tell them to reply JOIN to start onboarding. "
                "For check_status: tell them to reply STATUS. "
                "For cancel: tell them to reply RESTART. "
                "For help: explain the bot can connect them to plumbers (PLUMBER) or register them as one (JOIN). "
                "For question: answer briefly if you can, otherwise say a human will follow up. "
                "For greeting: greet back and explain the bot in one line. "
                "For chitchat: politely steer back to plumbing services."
            ),
        },
    },
    "required": ["intent", "job_type", "urgency", "location", "reply"],
    "additionalProperties": False,
}

NLU_SYSTEM_PROMPT = """You are the NLU (natural language understanding) layer for PlumberFlow, a WhatsApp bot in Johannesburg, South Africa that connects customers with plumbing problems to vetted plumbers in their area.

Your job: classify each incoming WhatsApp message into one of the allowed intents, extract any plumbing-job entities the user mentioned, and draft a short friendly WhatsApp reply.

Context about the bot:
- Customers reply *PLUMBER* to start a request, then walk through a 5-step flow (job type → urgency → location → photo → confirm).
- Plumbers reply *JOIN* to register. Leads are free to take; a flat R220 is due after a job is done and the plumber has been paid.
- Other commands: STATUS (check current job), RESTART (clear request), HELP (menu).
- Service area: Johannesburg suburbs (Randburg, Sandton, Fourways, Rosebank, Midrand, Soweto, etc.).
- Job types we handle: burst pipes, blocked drains, geyser problems, leaks, general plumbing.

Entity extraction guidance:
- job_type: map free text to the closest enum. "my pipe burst" → "Burst pipe". "drain is clogged" → "Blocked drain". "no hot water" / "geyser" → "Geyser problem". "dripping" / "leak" → "Leak". Unclear or other → "Other". If the user didn't describe an issue at all → null.
- urgency: "now" / "asap" / "emergency" / "burst" → "Emergency — right now". "today" / "this afternoon" → "Today". "this week" / "no rush" → "This week". "just want a price" / "quote" → "Just a quote". Not mentioned → null.
- location: extract the suburb name as the user wrote it (preserve case). Null if no location mentioned.

Reply tone: warm, brief, South African-English. No numbered menus. No emojis. Short sentences. Never make up plumber names, prices, or ETAs.

Return ONLY valid JSON matching the schema."""


async def classify_message(text: str) -> dict | None:
    """Run the NLU layer. Returns a dict with intent / entities / reply, or None on failure."""
    if not anthropic_client:
        return None
    try:
        response = await anthropic_client.messages.create(
            model="claude-haiku-4-5",
            max_tokens=400,
            system=[{
                "type": "text",
                "text": NLU_SYSTEM_PROMPT,
                "cache_control": {"type": "ephemeral"},  # cache the stable system prompt
            }],
            messages=[{"role": "user", "content": text}],
            output_config={"format": {"type": "json_schema", "schema": NLU_SCHEMA}},
        )
        text_block = next((b.text for b in response.content if b.type == "text"), None)
        if not text_block:
            return None
        result = json.loads(text_block)
        logger.info(f"NLU: {result.get('intent')} | job={result.get('job_type')} urg={result.get('urgency')} loc={result.get('location')}")
        return result
    except Exception as e:
        logger.warning(f"NLU classification failed: {e}")
        return None


JOB_REPLY_LABELS = ["sorted", "not_sorted", "never_came", "off_price", "other_off", "unrelated"]

JOB_REPLY_SCHEMA = {
    "type": "object",
    "properties": {
        "label": {"type": "string", "enum": JOB_REPLY_LABELS},
        "actual_value_rand": {
            "anyOf": [{"type": "number"}, {"type": "null"}],
            "description": "Only for off_price: the amount in rand the customer says they actually paid. Otherwise null.",
        },
    },
    "required": ["label", "actual_value_rand"],
    "additionalProperties": False,
}

JOB_REPLY_SYSTEM_PROMPT = """You classify a WhatsApp reply from a homeowner in Johannesburg about a plumbing job a plumber was sent to do. The homeowner was asked either "did the plumber get it sorted?" or "the plumber logged this price, does that sound right?".

Labels:
- sorted: the job was done and they are happy, or the price is fine.
- not_sorted: the plumber came but the job is not finished or not fixed.
- never_came: no plumber arrived, or nobody did any work.
- off_price: the price is wrong, or they were charged a different amount. Set actual_value_rand if they say what they paid.
- other_off: something else is wrong (rude, damage, poor work, a complaint that is not about price).
- unrelated: the message is about something else, for example asking for a new plumber, a question, or a greeting.

Return ONLY valid JSON matching the schema."""


async def classify_job_reply(text: str) -> tuple[str, int | None] | None:
    """(label, actual_value_cents) for a reply about a finished job, or None if the model is unavailable."""
    if not anthropic_client:
        return None
    try:
        response = await anthropic_client.messages.create(
            model="claude-haiku-4-5",
            max_tokens=100,
            system=[{
                "type": "text",
                "text": JOB_REPLY_SYSTEM_PROMPT,
                "cache_control": {"type": "ephemeral"},
            }],
            messages=[{"role": "user", "content": text}],
            output_config={"format": {"type": "json_schema", "schema": JOB_REPLY_SCHEMA}},
        )
        text_block = next((b.text for b in response.content if b.type == "text"), None)
        if not text_block:
            return None
        result = json.loads(text_block)
        value = result.get("actual_value_rand")
        cents = int(round(value * 100)) if isinstance(value, (int, float)) and value > 0 else None
        logger.info(f"Job reply label: {result.get('label')}")
        return result["label"], cents
    except Exception as e:
        logger.warning(f"Job reply classification failed: {e}")
        return None


async def handle_job_reply(sender: str, text: str) -> bool:
    """A customer with a job awaiting their answer: classify, then hand to verification."""
    quick = quick_job_reply_label(text)
    if quick:
        label, cents = quick, None
    else:
        result = await classify_job_reply(text)
        if not result:
            return False
        label, cents = result
    if label == "unrelated":
        return False
    return await db(S.ver.on_customer_reply, sender, label, actual_value_cents=cents)


def _job_awaiting_reply(phone: str) -> bool:
    # Reaches into the service so the model is only called when an answer is actually expected.
    return S.ver._job_awaiting_customer(phone) is not None


async def handle_nlu_result(sender: str, text: str, nlu: dict):
    """Act on the NLU result. Routes to the appropriate flow, or just sends the model's reply."""
    intent = nlu.get("intent")

    # Customer wants a plumber → start the lead flow, pre-filling anything we extracted.
    if intent == "request_plumber":
        state = {"stage": "job_type", "phone": sender}
        if nlu.get("job_type"):
            state["job_type"] = nlu["job_type"]
            state["stage"] = "urgency"
        if nlu.get("urgency") and "job_type" in state:
            state["urgency"] = nlu["urgency"]
            state["stage"] = "location"
        if nlu.get("location") and "urgency" in state:
            state["location"] = nlu["location"]
            state["stage"] = "photo"
        conversations[sender] = state

        # Send the model's friendly acknowledgment, then prompt for the next step.
        await send(sender, nlu.get("reply") or "Got it — let's find you a plumber.")
        await prompt_next_lead_step(sender, state)
        return

    # Plumber wants to join → kick off onboarding.
    if intent == "register_plumber":
        existing = await get_plumber(sender)
        if existing:
            await send(sender, f"You're already registered, {existing.get('first_name') or 'friend'}! 👷 Type *STATUS* or *HELP*.")
            return
        await start_plumber_onboarding(sender)
        return

    # Everything else: just send the model's reply. The user can then use the
    # explicit commands the reply nudges them toward.
    await send(sender, nlu.get("reply") or "👋 Reply *PLUMBER* if you need a plumber, or *JOIN* to register as one.")


async def prompt_next_lead_step(sender: str, state: dict):
    """Send the prompt for whatever stage of the lead flow we're now at."""
    stage = state.get("stage")
    if stage == "job_type":
        await send(sender, (
            "What's the issue — burst pipe, blocked drain, geyser, leak, or something else?"
        ))
    elif stage == "urgency":
        await send(sender, (
            "How urgent is it — do you need someone right now, today, this week, or just a quote?"
        ))
    elif stage == "location":
        await send(sender, "Which suburb are you in?")
    elif stage == "photo":
        await send(sender, (
            "Send a photo of the problem if you can — it helps the plumber quote accurately.\n\n"
            "Reply SKIP to continue without one."
        ))


# ===========================================================================
# MAIN MESSAGE ROUTER (customers, and anyone who is not a registered plumber)
# ===========================================================================
async def handle_text(sender: str, text: str):
    text_upper = text.strip().upper()
    text_lower = text.strip().lower()

    # 1. Onboarding in progress — must complete or cancel first
    if sender in plumber_onboarding:
        await handle_plumber_onboarding(sender, text)
        return

    # 2. JOIN / REGISTER keyword → start plumber onboarding
    if text_lower in JOIN_KEYWORDS:
        existing = await get_plumber(sender)
        if existing:
            await send(sender, (
                f"You're already registered, {existing.get('first_name') or 'friend'}! 👷\n\n"
                "Type *STATUS* to see your account.\n"
                "Type *HELP* to see all commands."
            ))
            return
        await start_plumber_onboarding(sender)
        return

    # 3. Admin commands (only from ADMIN_PHONE)
    if is_admin(sender) and await try_admin_command(sender, text_upper, text):
        return

    # 4. Customer commands (HELP, STATUS)
    if await try_customer_command(sender, text_upper):
        return

    # 5. If the sender is mid-flow already, continue the lead flow with their input.
    if sender in conversations:
        await continue_customer_flow(sender, text)
        return

    # 6. A customer with a finished job waiting on their answer (B1 / B2 follow-ups).
    if await db(_job_awaiting_reply, sender) and await handle_job_reply(sender, text):
        return

    # 7. NLU layer (Claude Haiku 4.5) — interpret free-text messages and route.
    #    Falls back to the default welcome if NLU is unavailable.
    nlu = await classify_message(text)
    if nlu:
        await handle_nlu_result(sender, text, nlu)
        return

    # 8. Final fallback: default welcome message.
    await continue_customer_flow(sender, text)

# ===========================================================================
# CUSTOMER COMMANDS
# ===========================================================================
async def try_customer_command(sender: str, text_upper: str) -> bool:
    if text_upper == "HELP":
        await send(sender, (
            "PlumberFlow commands:\n\n"
            "PLUMBER — request a plumber\n"
            "STATUS — check your current request\n"
            "RESTART — start a new request\n"
            "JOIN — register as a plumber in our network"
        ))
        return True

    if text_upper == "STATUS":
        req = await db(S.store.latest_request_for_customer, sender)
        if req:
            job_status, lead_status = req.get("job_status"), req["lead_status"]
            if job_status:
                label = "Plumber confirmed — they'll be in touch with you directly"
            elif lead_status == "skipped":
                label = "We couldn't place this one. Reply PLUMBER to try again."
            elif lead_status == "offered":
                label = "Sent to plumbers in your area, waiting for one to take it"
            else:
                label = "Our team is personally matching you"
            await send(sender, (
                f"Your request: {req.get('summary') or req.get('keyword') or 'N/A'} in {req.get('suburb') or 'N/A'}\n"
                f"Status: {label}"
            ))
        else:
            await send(sender, "No active request. Reply PLUMBER to start one.")
        return True

    if text_upper in ("RESTART", "RESET", "START OVER"):
        conversations.pop(sender, None)
        await send(sender, "Done. Reply PLUMBER to start a new request.")
        return True

    return False

# ===========================================================================
# PLUMBER MESSAGES that the take / done / no go handling did not claim
# ===========================================================================
async def forward_plumber_to_founder(phone: str, text: str):
    plumber = await get_plumber(phone)
    who = f"{plumber['first_name']} (+{normalize_phone(phone)})" if plumber else f"+{normalize_phone(phone)}"
    await notify_founder(f"Plumber {who}: {text}")

async def handle_plumber_text(phone: str, text: str):
    text_upper = text.strip().upper()

    if phone in plumber_command_state:
        await handle_plumber_command_state(phone, text)
        return

    if is_admin(phone) and await try_admin_command(phone, text_upper, text):
        return

    plumber = await get_plumber(phone)
    if plumber and text.strip().lower() in JOIN_KEYWORDS:
        await send(phone, f"You're already registered, {plumber.get('first_name') or 'friend'}! 👷\n\nType *HELP* to see all commands.")
        return
    if plumber and await try_plumber_command(phone, text_upper, plumber):
        return

    await forward_plumber_to_founder(phone, text)

# ===========================================================================
# PLUMBER COMMANDS
# ===========================================================================
async def try_plumber_command(sender: str, text_upper: str, plumber: dict) -> bool:
    if text_upper == "HELP":
        await send(sender, (
            "🔧 *PlumberFlow — Plumber Commands*\n\n"
            "• *STATUS* — your account & open jobs\n"
            "• *PAUSE* — stop receiving new leads\n"
            "• *RESUME* — start receiving leads again\n"
            "• *AREAS* — update the suburbs you cover\n"
            "• *SERVICES* — update the services you offer\n"
            "• *HELP* — show this menu\n\n"
            "When a job is finished, send *done*. If it fell through, send *no go*."
        ))
        return True

    if text_upper == "STATUS":
        holds = sorted(await db(S.store.get_holds, plumber["id"]))
        open_jobs = await db(S.store.open_job_count, plumber["id"])
        if holds:
            status = "⏸ Paused (" + ", ".join(HOLD_LABELS.get(h, h) for h in holds) + ")"
        else:
            status = "✅ Available"
        areas = ", ".join(a.title() for a in plumber.get("areas") or [])
        services = ", ".join(plumber.get("services") or [])
        await send(sender, (
            f"👷 *Your Account*\n\n"
            f"Name: {plumber.get('first_name') or '—'}\n"
            f"Status: {status}\n"
            f"Open jobs: {open_jobs}\n\n"
            f"📍 Areas: {areas}\n"
            f"🛠 Services: {services}\n\n"
            "Type *HELP* for all commands."
        ))
        return True

    if text_upper == "PAUSE":
        try:
            await db(S.store.add_hold, plumber["id"], "self_paused")
        except Exception:
            logger.exception("PAUSE failed (is migration 003 applied?)")
            await send(sender, "⚠️ Couldn't pause you just now. Try again shortly.")
            return True
        await send(sender, (
            "⏸ You're now *paused*. You won't be offered new leads until you type *RESUME*.\n\n"
            "Jobs you already have are still yours."
        ))
        return True

    if text_upper == "RESUME":
        await db(S.store.clear_hold, plumber["id"], "self_paused")
        holds = await db(S.store.get_holds, plumber["id"])
        if holds:
            await send(sender, (
                "Your own pause is off, but leads stay paused for: "
                + ", ".join(HOLD_LABELS.get(h, h) for h in sorted(holds)) + "."
            ))
        else:
            await send(sender, (
                "✅ You're back online! You'll start receiving leads matching your "
                "areas again."
            ))
        return True

    if text_upper == "AREAS":
        current = ", ".join(a.title() for a in plumber.get("areas") or [])
        plumber_command_state[sender] = {"command": "update_areas"}
        await send(sender, (
            f"📍 *Update your coverage areas*\n\n"
            f"Current: {current or '—'}\n\n"
            f"Reply with your new list of suburbs, separated by commas.\n"
            f"Example: *Randburg, Sandton, Fourways*\n\n"
            f"Or type *CANCEL* to keep your current areas."
        ))
        return True

    if text_upper == "SERVICES":
        current = ", ".join(plumber.get("services") or [])
        plumber_command_state[sender] = {"command": "update_services"}
        await send(sender, (
            f"🛠 *Update your services*\n\n"
            f"Current: {current or '—'}\n\n"
            f"Reply with numbers separated by commas:\n"
            f"1️⃣ Burst pipe\n"
            f"2️⃣ Blocked drain\n"
            f"3️⃣ Geyser problem\n"
            f"4️⃣ Leak\n"
            f"5️⃣ Other / General plumbing\n\n"
            f"Example: *1, 2, 4*\n\n"
            f"Or type *CANCEL* to keep your current services."
        ))
        return True

    return False

async def handle_plumber_command_state(sender: str, text: str):
    """Handle the second step of multi-step plumber commands."""
    state   = plumber_command_state[sender]
    command = state["command"]

    if text.upper() == "CANCEL":
        plumber_command_state.pop(sender, None)
        await send(sender, "✅ Cancelled. No changes made.")
        return

    plumber = await get_plumber(sender)
    if not plumber:
        plumber_command_state.pop(sender, None)
        return

    if command == "update_areas":
        areas = [a.strip().lower() for a in text.split(",") if a.strip()]
        if not areas:
            await send(sender, "Please list at least one suburb, or type *CANCEL*.")
            return
        plumber_command_state.pop(sender, None)
        try:
            await db(S.store.update_plumber, plumber["id"], areas=areas)
        except Exception:
            logger.exception("update areas failed")
            await send(sender, "⚠️ Something went wrong updating your areas. Please try again.")
            return
        await send(sender, f"✅ Your coverage areas are updated to:\n\n📍 {', '.join(a.title() for a in areas)}")
        return

    if command == "update_services":
        selected = [s.strip() for s in text.split(",")]
        services = [JOB_TYPES[n] for n in selected if n in JOB_TYPES]
        if not services:
            await send(sender, "Please reply with numbers 1–5 separated by commas, or type *CANCEL*.")
            return
        plumber_command_state.pop(sender, None)
        try:
            await db(S.store.update_plumber, plumber["id"], services=services)
        except Exception:
            logger.exception("update services failed")
            await send(sender, "⚠️ Something went wrong updating your services. Please try again.")
            return
        await send(sender, f"✅ Your services are updated to:\n\n🛠 {', '.join(services)}")
        return

# ===========================================================================
# ADMIN COMMANDS (WhatsApp, from ADMIN_PHONE)
# ===========================================================================
async def try_admin_command(sender: str, text_upper: str, raw_text: str) -> bool:
    if text_upper == "ADMIN" or text_upper == "ADMIN HELP":
        await send(sender, (
            "👑 *Admin Commands*\n\n"
            "• *STATS* — lead, job and plumber counts\n"
            "• *PLUMBERS* — list every registered plumber\n"
            "• *BROADCAST <message>* — send a message to all plumbers\n"
            "• *ADMIN* — show this menu\n\n"
            "Invoices and disputes are handled in Telegram: /unpaid /paid /disputes /resolve"
        ))
        return True

    if text_upper == "STATS":
        plumbers = await db(S.store.list_plumbers)
        counts = await db(S.store.counts)
        leads, jobs = counts["leads"], counts["jobs"]
        total_leads = sum(leads.values())
        taken = leads.get("taken", 0)
        rate = (taken / total_leads * 100) if total_leads else 0
        await send(sender, (
            f"📊 *PlumberFlow Stats*\n\n"
            f"*Plumbers*\n"
            f"• Total: {len(plumbers)}\n"
            f"• Active: {sum(1 for p in plumbers if p.get('active'))}\n\n"
            f"*Leads*\n"
            f"• Total: {total_leads}\n"
            f"• Open: {leads.get('open', 0)}\n"
            f"• Offered: {leads.get('offered', 0)}\n"
            f"• Taken: {taken}\n"
            f"• Skipped: {leads.get('skipped', 0)}\n"
            f"• Taken rate: {rate:.0f}%\n\n"
            f"*Jobs*\n"
            f"• Open: {jobs.get('accepted', 0) + jobs.get('awaiting_customer', 0)}\n"
            f"• Charged: {jobs.get('charged', 0)}\n"
            f"• No charge: {jobs.get('no_charge', 0)}\n"
            f"• Disputed: {jobs.get('disputed', 0)}\n"
            f"• Unresolved: {jobs.get('unresolved', 0)}"
        ))
        return True

    if text_upper == "PLUMBERS":
        plumbers = await db(S.store.list_plumbers)
        if not plumbers:
            await send(sender, "No plumbers registered yet.")
            return True
        lines = [f"👷 *Registered Plumbers ({len(plumbers)})*\n"]
        for p in plumbers:
            status_icon = "✅" if p.get("active") else "⏸"
            areas = ", ".join(a.title() for a in (p.get("areas") or []))[:50]
            lines.append(
                f"{status_icon} *{p.get('first_name') or '—'}* (+{normalize_phone(p.get('whatsapp'))})\n"
                f"   📍 {areas or '—'}\n"
            )
        # Split into chunks if too long (WhatsApp limit ~4096 chars)
        message = "\n".join(lines)
        for chunk in [message[i:i+3500] for i in range(0, len(message), 3500)]:
            await send(sender, chunk)
        return True

    if text_upper.startswith("BROADCAST "):
        broadcast_msg = raw_text[len("BROADCAST "):].strip()
        if not broadcast_msg:
            await send(sender, "Usage: *BROADCAST your message here*")
            return True
        plumbers = await db(S.store.list_plumbers)
        sent_count = 0
        for p in plumbers:
            if not p.get("whatsapp") or normalize_phone(p["whatsapp"]) == normalize_phone(sender):
                continue
            try:
                await db(S.messenger.send_plumber, p, f"📢 *Message from PlumberFlow*\n\n{broadcast_msg}")
                sent_count += 1
            except Exception as e:
                logger.error(f"Broadcast to {p.get('first_name')} failed: {e}")
        await send(sender, f"✅ Broadcast sent to {sent_count} plumber(s).")
        return True

    return False

# ===========================================================================
# CUSTOMER LEAD FLOW
# ===========================================================================
async def _is_returning_customer(sender: str) -> bool:
    """True if this number has had a job before."""
    return await db(S.store.customer_has_job, sender)

async def continue_customer_flow(sender: str, text: str):
    state = conversations.get(sender)
    text_lower  = text.strip().lower()
    text_number = text.strip()

    if state is None:
        # Only start the lead flow if they explicitly asked for a plumber.
        if text_lower in PLUMBER_KEYWORDS or text_lower == "plumber":
            returning = await _is_returning_customer(sender)
            conversations[sender] = {"stage": "job_type", "phone": sender}
            if returning:
                await send(sender, "Good to hear from you again. What's the problem this time?")
            else:
                await send(sender, "I find plumbers across Johannesburg. What's the problem?")
            return

        # Not a recognised trigger — brief welcome.
        await send(sender, (
            "I find plumbers in Johannesburg.\n\n"
            "Need one? Reply PLUMBER and I'll match you with someone in your area.\n\n"
            "Are you a plumber? Reply JOIN to register and start getting leads."
        ))
        return

    stage = state["stage"]

    if stage == "job_type":
        # Accept numeric shortcut (1-5) or free text
        job = JOB_TYPES.get(text_number) or parse_job_type_text(text)
        if not job:
            # Last resort: ask Haiku
            nlu = await classify_message(text)
            job = nlu.get("job_type") if nlu else None
        if not job:
            await send(sender, (
                "I didn't catch that. What's the issue — burst pipe, blocked drain, "
                "geyser, leak, or something else?"
            ))
            return
        state["job_type"] = job
        state["stage"]    = "urgency"
        await send(sender, (
            "How urgent is it — do you need someone right now, today, this week, or just a quote?"
        ))

    elif stage == "urgency":
        # Accept numeric shortcut (1-4) or natural language
        urgency = URGENCY_LEVELS.get(text_number) or parse_urgency_text(text)
        if not urgency:
            nlu = await classify_message(text)
            urgency = nlu.get("urgency") if nlu else None
        if not urgency:
            await send(sender, (
                "How urgent is it — right now, today, this week, or just a quote?"
            ))
            return
        state["urgency"] = urgency
        state["stage"]   = "location"
        await send(sender, "Which suburb are you in?")

    elif stage == "location":
        state["location"] = text.strip()
        state["stage"]    = "photo"
        await send(sender, (
            "Send a photo of the problem if you can — it helps the plumber quote accurately.\n\n"
            "Reply SKIP to continue without one."
        ))

    elif stage == "photo":
        if text_lower in ("skip", "no", "nope", "none", "no photo"):
            state["photo"] = None
            await finish_lead(sender, state)
        else:
            await send(sender, "Send a photo, or reply SKIP to continue without one.")

async def handle_image(sender: str, media_id: str):
    state = conversations.get(sender)
    if state and state.get("stage") == "photo":
        state["photo"] = media_id
        await finish_lead(sender, state)
    elif state is None:
        conversations[sender] = {"stage": "job_type", "phone": sender}
        await send(sender, (
            "Got it — I can see your photo. What's the issue — burst pipe, blocked drain, "
            "geyser, leak, or something else?"
        ))

# ===========================================================================
# PLUMBER ONBOARDING (6-step flow)
# ===========================================================================
async def start_plumber_onboarding(sender: str):
    plumber_onboarding[sender] = {"stage": "ob_name", "phone": sender}
    await send(sender, (
        "👷 Welcome to *PlumberFlow*!\n\n"
        "We connect verified plumbers with customers in your area. "
        "You'll be offered job leads on WhatsApp and reply *take* to claim one.\n\n"
        "Let's get you set up — it takes 2 minutes.\n\n"
        "━━━━━━━━━━━━━━━\n"
        "Step 1 of 6\n\n"
        "What's your first name?"
    ))

async def handle_plumber_onboarding(sender: str, text: str):
    state = plumber_onboarding[sender]
    stage = state["stage"]

    if text.lower() in ("cancel", "exit", "quit"):
        plumber_onboarding.pop(sender, None)
        await send(sender, "No problem! Send JOIN any time to register.")
        return

    if stage == "ob_name":
        first = text.strip().split(" ")[0][:40].title()
        if not first or not first.replace("-", "").replace("'", "").isalpha():
            await send(sender, "Please reply with your first name, for example *Sipho*.")
            return
        state["first_name"] = first
        state["stage"] = "ob_experience"
        await send(sender, (
            "━━━━━━━━━━━━━━━\n"
            "Step 2 of 6\n\n"
            "How many years of plumbing experience do you have?\n\n"
            "Also, are you PIRB registered?\n"
            "Reply in this format:\n"
            "👉 *5, PIRB12345*  (years, then PIRB number)\n"
            "👉 *3, No PIRB*    (years, then 'No PIRB' if not registered)"
        ))

    elif stage == "ob_experience":
        parts = [p.strip() for p in text.split(",", 1)]
        if not parts[0].isdigit():
            await send(sender, "Please reply with your years of experience and PIRB status.\nExample: *5, PIRB12345* or *3, No PIRB*")
            return
        state["years_experience"] = int(parts[0])
        pirb_part = parts[1].strip() if len(parts) > 1 else ""
        if pirb_part.lower().startswith("no") or pirb_part == "":
            state["pirb_registered"] = False
            state["pirb_number"]     = None
        else:
            state["pirb_registered"] = True
            state["pirb_number"]     = pirb_part.upper().replace("PIRB", "PIRB ").strip()
        state["stage"] = "ob_services"
        await send(sender, (
            "━━━━━━━━━━━━━━━\n"
            "Step 3 of 6\n\n"
            "Which services do you offer?\n\n"
            "Reply with the numbers separated by commas:\n\n"
            "1️⃣ Burst pipe\n"
            "2️⃣ Blocked drain\n"
            "3️⃣ Geyser problem\n"
            "4️⃣ Leak\n"
            "5️⃣ Other / General plumbing\n\n"
            "Example: *1, 2, 4*  or  *1, 2, 3, 4, 5*"
        ))

    elif stage == "ob_services":
        selected = [s.strip() for s in text.split(",")]
        job_types = [JOB_TYPES[n] for n in selected if n in JOB_TYPES]
        if not job_types:
            await send(sender, "Please reply with numbers 1–5 separated by commas. Example: *1, 3, 4*")
            return
        state["job_types"] = job_types
        state["stage"]     = "ob_area"
        await send(sender, (
            "━━━━━━━━━━━━━━━\n"
            "Step 4 of 6\n\n"
            "📍 Which suburbs do you cover?\n\n"
            "List them separated by commas.\n"
            "Example: *Randburg, Sandton, Rosebank, Fourways*"
        ))

    elif stage == "ob_area":
        areas = [a.strip().lower() for a in text.split(",") if a.strip()]
        if not areas:
            await send(sender, "Please list at least one suburb. Example: *Randburg, Sandton*")
            return
        state["areas"] = areas
        state["stage"] = "ob_availability"
        await send(sender, (
            "━━━━━━━━━━━━━━━\n"
            "Step 5 of 6\n\n"
            "🕐 What are your working hours?\n\n"
            "1️⃣ Mon–Fri, 8am–5pm\n"
            "2️⃣ Mon–Sat, 8am–6pm\n"
            "3️⃣ 24/7 including emergencies\n"
            "4️⃣ Weekends only\n\n"
            "Or type your own hours (e.g. *Mon–Fri 7am–4pm*)"
        ))

    elif stage == "ob_availability":
        availability = AVAILABILITY_OPTIONS.get(text) or text
        state["availability"] = availability
        state["stage"]        = "ob_confirmation"
        pirb_display  = state["pirb_number"] if state.get("pirb_registered") else "Not registered"
        areas_display = ", ".join(a.title() for a in state["areas"])
        services_display = ", ".join(state["job_types"])
        await send(sender, (
            "━━━━━━━━━━━━━━━\n"
            "Step 6 of 6 — Please confirm your details:\n\n"
            f"👤 Name: {state['first_name']}\n"
            f"📱 Phone: +{sender}\n"
            f"🔧 Experience: {state['years_experience']} years\n"
            f"📋 PIRB: {pirb_display}\n"
            f"🛠 Services: {services_display}\n"
            f"📍 Areas: {areas_display}\n"
            f"🕐 Availability: {availability}\n\n"
            "━━━━━━━━━━━━━━━\n"
            "Free to take. You only pay after a job is done and paid.\n\n"
            "Reply *YES* to join PlumberFlow or *NO* to cancel."
        ))

    elif stage == "ob_confirmation":
        if text.upper() == "YES":
            await finish_plumber_onboarding(sender, state)
        elif text.upper() == "NO":
            plumber_onboarding.pop(sender, None)
            await send(sender, "No problem! Send JOIN any time if you change your mind. 👍")
        else:
            await send(sender, "Please reply *YES* to confirm or *NO* to cancel.")

async def finish_plumber_onboarding(sender: str, state: dict):
    plumber_onboarding.pop(sender, None)
    record = {
        "first_name":       state["first_name"],
        "whatsapp":         sender,
        "experience_years": state["years_experience"],
        "pirb_registered":  state["pirb_registered"],
        "services":         state["job_types"],
        "areas":            state["areas"],
        "availability":     state["availability"],
    }
    saved = False
    try:
        await db(S.store.insert_plumber, record)
        saved = True
        logger.info(f"New plumber saved: {sender}")
    except Exception as e:
        logger.error(f"Plumber insert failed for {sender}: {e}")
        logger.info(f"NEW PLUMBER (manual add needed): {record}")

    areas_display = ", ".join(a.title() for a in state["areas"])
    await send(sender, (
        "🎉 Welcome to PlumberFlow!\n\n"
        "You're now in our network. Here's what happens next:\n\n"
        "✅ Free to take — you only pay after a job is done and paid\n"
        f"📍 Covering: {areas_display}\n\n"
        "When a customer in your area needs a plumber, you'll get a "
        "WhatsApp message with the suburb and the problem.\n\n"
        "Reply *take* to claim it. The first plumber to reply gets the job.\n\n"
        "When the job is finished, send *done*. If it fell through, send *no go*.\n\n"
        "Type *HELP* any time to see all commands.\n\n"
        "Good luck! 🔧"
    ))

    pirb_display = state.get("pirb_number") or "Not registered"
    await notify_founder(
        f"👷 NEW PLUMBER JOINED\n\n"
        f"Name: {state['first_name']}\n"
        f"Phone: +{sender}\n"
        f"Experience: {state['years_experience']} years\n"
        f"PIRB: {pirb_display}\n"
        f"Services: {', '.join(state['job_types'])}\n"
        f"Areas: {areas_display}\n"
        f"Availability: {state['availability']}\n"
        f"Saved: {'yes' if saved else 'FAILED — add manually'}"
    )

# ===========================================================================
# LEAD CAPTURE → DISPATCH
# ===========================================================================
async def finish_lead(sender: str, state: dict):
    # The conversation ends here, so the customer's next message (a follow-up answer, or
    # another request) is routed normally instead of being swallowed by a finished flow.
    conversations.pop(sender, None)
    logger.info(f"NEW LEAD: {state}")

    urgency   = state.get("urgency", "Today")
    tier_info = get_tier_info(urgency)
    job_type  = state.get("job_type", "Other")
    short     = job_short_name(job_type)
    location  = state.get("location", "your area")
    summary   = f"{job_type} · {urgency}" + (" · photo sent" if state.get("photo") else "")

    try:
        await db(
            S.disp.new_lead,
            customer_phone=sender, customer_name=profile_names.get(sender),
            suburb=location, keyword=short, address=None, summary=summary,
        )
    except Exception:
        logger.exception("dispatch.new_lead failed")
        await send(sender, "Something went wrong on our side logging that. Please reply PLUMBER to send it again.")
        return

    # Customer confirmation — urgency-aware
    msg = f"I've logged your {short} in {location}."
    if tier_info["self_help"]:
        msg += f"\n\n{tier_info['self_help']}"
    msg += f"\n\nWe'll find you a plumber and be in touch within {tier_info['eta']}."
    await send(sender, msg)

# ---------------------------------------------------------------------------
# Health check
# ---------------------------------------------------------------------------
@app.get("/health")
async def health():
    return {
        "status":    "ok",
        "service":   "plumberflow",
        "services":  "ready" if S else "not started",
        "scheduler": "running" if _scheduler and _scheduler.running else "stopped",
        "admin":     "configured" if ADMIN_PHONE else "not configured",
    }
