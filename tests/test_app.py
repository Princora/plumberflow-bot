"""Wiring tests for main.py: webhook routing, Telegram auth, dispatch and verification
hand-offs, plumber commands, founder commands. Real services, fake HTTP, in-memory store.
The SQL behind AppStore is checked separately against Postgres."""

import itertools
import re
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

import main
from dispatch import DispatchService
from dispatch.store import InMemoryDispatchStore
from dispatch.telegram import TelegramClient
from dispatch.whatsapp import CloudMessenger, WhatsAppCloud
from services import Services
from verification import VerificationService

FOUNDER = 999
CUSTOMER = "27829990000"
PLUMBER_PHONE = "27821110000"


class MemoryAppStore(InMemoryDispatchStore):
    """InMemoryDispatchStore plus the AppStore methods main.py calls."""

    def close(self):
        pass

    def find_plumber_by_whatsapp(self, phone):
        from dispatch.phone import normalize_phone
        want = normalize_phone(phone)
        return next((p for p in self.plumbers.values()
                     if normalize_phone(p.get("whatsapp")) == want), None)

    def list_plumbers(self):
        return list(self.plumbers.values())

    def insert_plumber(self, row):
        return self.add_plumber(row["first_name"], **{k: v for k, v in row.items() if k != "first_name"})

    def update_plumber(self, plumber_id, **fields):
        self.plumbers[plumber_id].update(fields)

    def open_job_count(self, plumber_id):
        return sum(1 for j in self.jobs.values()
                   if j["plumber_id"] == plumber_id and j["status"] in ("accepted", "awaiting_customer"))

    def latest_request_for_customer(self, phone):
        leads = [l for l in self.leads.values() if l["customer_phone"] == phone]
        if not leads:
            return None
        lead = leads[-1]
        job = self.get_job_by_lead(lead["ref"])
        return {"ref": lead["ref"], "lead_status": lead["status"], "suburb": lead["suburb"],
                "keyword": lead["keyword"], "summary": lead["summary"],
                "job_status": job["status"] if job else None}

    def customer_has_job(self, phone):
        return any(j["customer_phone"] == phone for j in self.jobs.values())

    def counts(self):
        leads, jobs = {}, {}
        for l in self.leads.values():
            leads[l["status"]] = leads.get(l["status"], 0) + 1
        for j in self.jobs.values():
            jobs[j["status"]] = jobs.get(j["status"], 0) + 1
        return {"leads": leads, "jobs": jobs}

    def _prefix(self, rows, prefix):
        hits = [r for r in rows if r["id"].startswith(prefix)]
        return hits[0] if len(hits) == 1 else None

    def find_invoice_by_prefix(self, prefix):
        return self._prefix(self.invoices.values(), prefix)

    def find_job_by_prefix(self, prefix):
        return self._prefix(self.jobs.values(), prefix)

    def list_unpaid_invoices(self):
        return [dict(i, first_name=(self.plumbers.get(i["plumber_id"]) or {}).get("first_name"))
                for i in self.invoices.values() if i["status"] == "sent"]

    def list_open_disputes(self):
        return [dict(j, first_name=(self.plumbers.get(j["plumber_id"]) or {}).get("first_name"))
                for j in self.jobs.values() if j["status"] in ("disputed", "unresolved")]


class Env:
    def __init__(self):
        self.wa_calls = []        # (to, kind, text)
        self.tg_calls = []        # (method, payload)
        self._tg_id = itertools.count(100)
        self._wa_id = itertools.count(1)
        self.store = MemoryAppStore()
        wa = WhatsAppCloud("T", "PNID", post=self._wa_post)
        tg = TelegramClient("T", 111, post=self._tg_post)
        self.messenger = CloudMessenger(wa, tg)
        ver = VerificationService(self.store, self.messenger)
        disp = DispatchService(self.store, ver, self.messenger, tg)
        self.services = Services(self.store, wa, tg, self.messenger, ver, disp, FOUNDER)
        self.client = None

    def _wa_post(self, url, token, payload):
        if payload["type"] == "text":
            self.wa_calls.append((payload["to"], "text", payload["text"]["body"]))
        elif payload["type"] == "interactive":
            self.wa_calls.append((payload["to"], "buttons", payload["interactive"]["body"]["text"]))
        return {}

    def _tg_post(self, url, payload):
        method = url.rsplit("/", 1)[-1]
        self.tg_calls.append((method, payload))
        return {"result": {"message_id": next(self._tg_id)}}

    # helpers
    def wa_text_to(self, phone):
        return [t for to, kind, t in self.wa_calls if to == phone and kind == "text"]

    def tg_texts(self):
        return [p.get("text", "") for m, p in self.tg_calls if m in ("sendMessage", "editMessageText")]

    def post_wa(self, phone, message: dict | None = None, text=None, name="Thandi Mokoena", mid=None):
        mid = mid or f"wamid.{next(self._wa_id)}"
        msg = message or {"from": phone, "id": mid, "type": "text", "text": {"body": text}}
        msg.setdefault("from", phone)
        msg.setdefault("id", mid)
        body = {"entry": [{"changes": [{"value": {
            "contacts": [{"wa_id": phone, "profile": {"name": name}}], "messages": [msg]}}]}]}
        r = self.client.post("/webhook", json=body)
        assert r.status_code == 200
        return r

    def post_tg(self, update, secret=None):
        headers = {"X-Telegram-Bot-Api-Secret-Token": secret} if secret else {}
        return self.client.post("/telegram", json=update, headers=headers)

    def founder_tap(self, data):
        return self.post_tg({"callback_query": {"id": "cb1", "from": {"id": FOUNDER}, "data": data}})

    def founder_says(self, text):
        return self.post_tg({"message": {"from": {"id": FOUNDER}, "chat": {"id": 111}, "text": text}})

    def add_plumber(self, name="Sipho", phone=PLUMBER_PHONE, areas=("randburg",)):
        return self.store.add_plumber(name, whatsapp=phone, areas=list(areas), services=["Burst pipe"])

    def run_lead_flow(self, phone=CUSTOMER, suburb="Randburg"):
        for text in ("plumber", "burst pipe", "now", suburb, "skip"):
            self.post_wa(phone, text=text)


@pytest.fixture
def env(monkeypatch):
    e = Env()
    monkeypatch.setattr(main, "S", e.services)
    monkeypatch.setattr(main, "anthropic_client", None)
    monkeypatch.setattr(main, "TELEGRAM_WEBHOOK_SECRET", "")
    monkeypatch.setattr(main, "ADMIN_PHONE", "")
    for state in (main.conversations, main.plumber_onboarding, main.plumber_command_state,
                  main.profile_names, main._seen_message_ids):
        state.clear()
    with TestClient(main.app) as client:
        e.client = client
        yield e


def p8(plumber):
    return plumber["id"].replace("-", "")[:8]


# ───────────────────────── lead capture → founder ─────────────────────────

def test_finished_lead_is_posted_to_founder_with_buttons(env):
    sipho = env.add_plumber()
    env.run_lead_flow()
    assert len(env.store.leads) == 1
    lead = next(iter(env.store.leads.values()))
    assert lead["customer_name"] == "Thandi"            # first name from the WhatsApp profile
    assert lead["suburb"] == "Randburg" and lead["keyword"] == "burst pipe"
    sends = [p for m, p in env.tg_calls if m == "sendMessage"]
    assert sends and "L-0001" in sends[-1]["text"]
    buttons = [b["callback_data"] for row in sends[-1]["reply_markup"]["inline_keyboard"] for b in row]
    assert f"o:L-0001:{p8(sipho)}" in buttons
    assert any("logged your burst pipe" in t for t in env.wa_text_to(CUSTOMER))


# ───────────────────────── Telegram ─────────────────────────

def test_founder_offer_then_plumber_take_creates_job(env):
    sipho = env.add_plumber()
    env.run_lead_flow()
    env.founder_tap(f"o:L-0001:{p8(sipho)}")
    offers = env.wa_text_to(PLUMBER_PHONE)
    assert offers and "New job — Randburg" in offers[-1] and "R220" in offers[-1]

    env.post_wa(PLUMBER_PHONE, text="take")
    job = env.store.get_job_by_lead("L-0001")
    assert job and job["plumber_id"] == sipho["id"] and job["customer_phone"] == CUSTOMER
    assert any("You've got it." in t and CUSTOMER in t for t in env.wa_text_to(PLUMBER_PHONE))
    assert env.store.leads["L-0001"]["status"] == "taken"


def test_buttons_from_anyone_but_the_founder_do_nothing(env):
    sipho = env.add_plumber()
    env.run_lead_flow()
    env.post_tg({"callback_query": {"id": "cb", "from": {"id": 12345}, "data": f"o:L-0001:{p8(sipho)}"}})
    assert env.store.offers == {}
    assert env.wa_text_to(PLUMBER_PHONE) == []


def test_telegram_secret_is_enforced_when_configured(env, monkeypatch):
    monkeypatch.setattr(main, "TELEGRAM_WEBHOOK_SECRET", "s3cret")
    assert env.post_tg({}, secret=None).status_code == 403
    assert env.post_tg({}, secret="wrong").status_code == 403
    assert env.post_tg({}, secret="s3cret").status_code == 200


def test_paused_plumber_needs_offer_anyway(env):
    sipho = env.add_plumber()
    env.store.add_hold(sipho["id"], "unpaid_invoice")
    env.run_lead_flow()
    env.founder_tap(f"o:L-0001:{p8(sipho)}")
    assert env.wa_text_to(PLUMBER_PHONE) == []                       # not offered
    assert any("Offer anyway?" in t for t in env.tg_texts())


# ───────────────────────── WhatsApp routing ─────────────────────────

def test_plumber_text_nobody_claims_goes_to_the_founder(env):
    env.add_plumber()
    env.post_wa(PLUMBER_PHONE, text="my bakkie broke down, can't make it")
    assert any("Plumber Sipho" in t and "bakkie" in t for t in env.tg_texts())
    assert env.wa_text_to(PLUMBER_PHONE) == []                       # not treated as a customer
    assert main.conversations == {}


def test_plumber_photo_is_forwarded_not_treated_as_a_customer_job(env):
    env.add_plumber()
    env.post_wa(PLUMBER_PHONE, message={"type": "image", "image": {"id": "media1"}})
    assert any("sent a photo" in t for t in env.tg_texts())
    assert main.conversations == {}


def test_duplicate_webhook_delivery_is_processed_once(env):
    env.post_wa(CUSTOMER, text="help", mid="wamid.same")
    env.post_wa(CUSTOMER, text="help", mid="wamid.same")
    assert len([t for t in env.wa_text_to(CUSTOMER) if "PlumberFlow commands" in t]) == 1


def test_one_failing_message_does_not_fail_the_webhook(env, monkeypatch):
    async def boom(*a, **k):
        raise RuntimeError("db down")
    monkeypatch.setattr(main, "handle_text", boom)
    assert env.post_wa(CUSTOMER, text="help").status_code == 200


# ───────────────────────── plumber commands ─────────────────────────

def test_plumber_pause_and_resume_use_a_hold_not_the_active_flag(env):
    sipho = env.add_plumber()
    env.post_wa(PLUMBER_PHONE, text="PAUSE")
    assert env.store.get_holds(sipho["id"]) == {"self_paused"}
    assert env.store.plumbers[sipho["id"]]["active"] is True          # still recognised for "done"
    env.post_wa(PLUMBER_PHONE, text="status")
    assert any("Paused (you paused)" in t for t in env.wa_text_to(PLUMBER_PHONE))
    env.post_wa(PLUMBER_PHONE, text="RESUME")
    assert env.store.get_holds(sipho["id"]) == set()


def test_plumber_updates_areas(env):
    sipho = env.add_plumber()
    env.post_wa(PLUMBER_PHONE, text="AREAS")
    env.post_wa(PLUMBER_PHONE, text="Fourways, Northcliff")
    assert env.store.plumbers[sipho["id"]]["areas"] == ["fourways", "northcliff"]


def test_new_plumber_onboarding_saves_a_row_and_tells_the_founder(env):
    phone = "27835550000"
    for text in ("join", "Lerato", "7, PIRB123", "1, 2", "Randburg, Sandton", "1", "YES"):
        env.post_wa(phone, text=text)
    saved = env.store.find_plumber_by_whatsapp(phone)
    assert saved["first_name"] == "Lerato" and saved["experience_years"] == 7
    assert saved["services"] == ["Burst pipe", "Blocked drain"]
    assert saved["areas"] == ["randburg", "sandton"]
    assert any("NEW PLUMBER JOINED" in t and "Lerato" in t for t in env.tg_texts())


# ───────────────────────── verification hand-offs ─────────────────────────

def taken_job(env):
    sipho = env.add_plumber()
    env.run_lead_flow()
    env.founder_tap(f"o:L-0001:{p8(sipho)}")
    env.post_wa(PLUMBER_PHONE, text="take")
    return sipho, env.store.get_job_by_lead("L-0001")


def test_plumber_done_with_amount_is_logged(env):
    _, job = taken_job(env)
    env.post_wa(PLUMBER_PHONE, text="done R2400")
    job = env.store.get_job(job["id"])
    assert job["plumber_claim"] == "done" and job["job_value_cents"] == 240000
    assert any(t.startswith("Logged — R2,400") for t in env.wa_text_to(PLUMBER_PHONE))


def test_customer_reply_after_the_check_is_classified_and_reaches_verification(env):
    _, job = taken_job(env)
    env.post_wa(PLUMBER_PHONE, text="done R2400")
    env.store.update_job(job["id"], b1_sent_at=datetime.now(timezone.utc))
    env.post_wa(CUSTOMER, text="he never came")
    job = env.store.get_job(job["id"])
    assert job["customer_response"] == "never_came"
    assert any("HIGH SIGNAL" in t for t in env.tg_texts())
    assert any("That's not okay" in t for t in env.wa_text_to(CUSTOMER))


def test_customer_who_is_not_being_asked_anything_still_gets_the_normal_bot(env):
    taken_job(env)
    env.post_wa(CUSTOMER, text="help")
    assert any("PlumberFlow commands" in t for t in env.wa_text_to(CUSTOMER))


def test_customer_can_ask_for_another_plumber_while_a_check_is_open(env):
    _, job = taken_job(env)
    env.store.update_job(job["id"], b2_sent_at=datetime.now(timezone.utc))
    env.post_wa(CUSTOMER, text="plumber")
    assert main.conversations[CUSTOMER]["stage"] == "job_type"


# ───────────────────────── founder commands ─────────────────────────

def test_founder_marks_an_invoice_paid_by_short_id(env):
    sipho = env.add_plumber()
    inv = env.store.insert_invoice({
        "plumber_id": sipho["id"], "week_start": datetime(2026, 10, 5).date(), "job_count": 1,
        "total_cents": 22000, "status": "sent", "sent_at": datetime.now(timezone.utc)})
    env.founder_says("/unpaid")
    assert any(inv["id"][:8] in t and "Sipho" in t for t in env.tg_texts())
    env.founder_says(f"/paid {inv['id'][:8]}")
    assert env.store.invoices[inv["id"]]["status"] == "paid"


def test_founder_resolves_a_dispute(env):
    sipho, job = taken_job(env)
    env.store.update_job(job["id"], status="disputed")
    env.founder_says("/disputes")
    assert any(job["id"][:8] in t for t in env.tg_texts())
    env.founder_says(f"/resolve {job['id'][:8]} no_charge customer confirmed it never happened")
    assert env.store.get_job(job["id"])["status"] == "no_charge"


def test_founder_commands_from_anyone_else_are_ignored(env):
    env.post_tg({"message": {"from": {"id": 5}, "chat": {"id": 5}, "text": "/unpaid"}})
    assert env.tg_calls == []


# ───────────────────────── scheduler ─────────────────────────

def test_a_failing_sweep_does_not_stop_the_others(env, monkeypatch):
    ran = []
    monkeypatch.setattr(env.services.ver, "sweep_jobs", lambda: (_ for _ in ()).throw(RuntimeError("meta refused")))
    monkeypatch.setattr(env.services.ver, "sweep_patterns", lambda: ran.append("patterns"))
    monkeypatch.setattr(env.services.ver, "sweep_invoices", lambda: ran.append("invoices"))
    monkeypatch.setattr(env.services.disp, "run_sweeps", lambda: ran.append("dispatch"))
    main.run_sweeps()
    assert ran == ["patterns", "invoices", "dispatch"]
