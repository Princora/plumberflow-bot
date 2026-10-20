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

    def find_plumber_by_prefix(self, prefix):
        return self._prefix([dict(p, id=str(p["id"])) for p in self.plumbers.values()], prefix)

    def list_pending_plumbers(self):
        pending = {pid for pid, reason in self.holds if reason == "pending_approval"}
        return [p for p in self.plumbers.values() if p["id"] in pending]

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
    assert any("WAITING FOR APPROVAL" in t and "Lerato" in t for t in env.tg_texts())


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


# ───────────────────────── suburbs and urgency ─────────────────────────

import json
from types import SimpleNamespace


NLU_ANSWER = {"You are the NLU": {"intent": "request_plumber", "job_type": None, "urgency": None,
                                  "location": None, "reply": "Sure."}}


class FakeAnthropic:
    """Stands in for AsyncAnthropic. `answers` maps a phrase in the system prompt to the JSON to return."""

    def __init__(self, answers):
        self.answers, self.calls = answers, []
        self.messages = self

    async def create(self, **kw):
        system = kw["system"][0]["text"]
        user = kw["messages"][0]["content"]
        self.calls.append((system[:140], user))
        for phrase, reply in self.answers.items():
            if phrase in system:
                body = reply(user) if callable(reply) else reply
                return SimpleNamespace(content=[SimpleNamespace(type="text", text=json.dumps(body))])
        raise AssertionError("unexpected model call")


def lead(env):
    return next(iter(env.store.leads.values()))


def suggested(env):
    sends = [p for m, p in env.tg_calls if m == "sendMessage"]
    return [b["callback_data"] for row in sends[-1]["reply_markup"]["inline_keyboard"] for b in row]


def test_customer_suburb_with_an_extension_still_finds_the_plumber(env):
    sipho = env.add_plumber(areas=("rivonia",))
    env.run_lead_flow(suburb="Rivonia Ext 3")
    assert lead(env)["suburb"] == "Rivonia"
    assert f"o:L-0001:{p8(sipho)}" in suggested(env)


def test_plumber_areas_typed_any_old_way_still_match(env):
    sipho = env.add_plumber(areas=("Rivonia.", " HYDEPARK ", "fourways"))
    env.run_lead_flow(suburb="Hyde Park")
    assert f"o:L-0001:{p8(sipho)}" in suggested(env)


def test_an_unrecognised_suburb_is_asked_once_then_taken_as_typed(env):
    env.add_plumber()
    for text in ("plumber", "burst pipe", "now", "Alexandra"):
        env.post_wa(CUSTOMER, text=text)
    assert "I didn't recognise that suburb" in env.wa_text_to(CUSTOMER)[-1]
    assert main.conversations[CUSTOMER]["stage"] == "location"
    env.post_wa(CUSTOMER, text="Alexandra")
    assert main.conversations[CUSTOMER]["stage"] == "photo"
    env.post_wa(CUSTOMER, text="skip")
    assert lead(env)["suburb"] == "Alexandra"          # not lost; the founder routes it


def test_the_model_can_fix_a_typo_but_only_to_a_real_suburb(env, monkeypatch):
    env.add_plumber(areas=("sandton",))
    fake = FakeAnthropic({**NLU_ANSWER, "match what a person typed": lambda t: {"suburb": "Sandton" if t == "Sanderton" else "Atlantis"}})
    monkeypatch.setattr(main, "anthropic_client", fake)
    env.run_lead_flow(suburb="Sanderton")
    assert lead(env)["suburb"] == "Sandton"

    # An invented suburb is rejected, so the customer is asked rather than sent to the wrong place.
    for text in ("plumber", "burst pipe", "now", "Gotham"):
        env.post_wa("27829991111", text=text)
    assert "I didn't recognise that suburb" in env.wa_text_to("27829991111")[-1]


def test_the_model_is_not_asked_when_the_suburb_is_clear(env, monkeypatch):
    env.add_plumber()
    fake = FakeAnthropic(NLU_ANSWER)
    monkeypatch.setattr(main, "anthropic_client", fake)
    env.run_lead_flow(suburb="Randburg")
    assert [c for c in fake.calls if "match what a person typed" in c[0] or "suburb" in c[0].lower()] == []
    assert lead(env)["suburb"] == "Randburg"


def test_plumber_area_list_is_canonicalised_and_unknown_places_are_flagged(env):
    sipho = env.add_plumber()
    env.post_wa(PLUMBER_PHONE, text="AREAS")
    env.post_wa(PLUMBER_PHONE, text="Randburg and Sandton, hydepark, Rivonia Ext 3, Alexandra")
    assert env.store.plumbers[sipho["id"]]["areas"] == [
        "randburg", "sandton", "hyde park", "rivonia", "alexandra"]
    sent = env.wa_text_to(PLUMBER_PHONE)
    assert any("Alexandra" in t and "on my suburb list" in t for t in sent)
    assert not any("Randburg" in t and "on my suburb list" in t for t in sent)


def test_onboarding_canonicalises_areas(env):
    phone = "27835550000"
    for text in ("join", "Lerato", "7, PIRB123", "1, 2", "Rivonia Ext 3, fourwys", "1", "YES"):
        env.post_wa(phone, text=text)
    assert env.store.find_plumber_by_whatsapp(phone)["areas"] == ["rivonia", "fourways"]


def urgency_of(env):
    for text in ("plumber", "burst pipe"):
        env.post_wa(CUSTOMER, text=text)


@pytest.mark.parametrize("answer", ["not urgent", "it's not an emergency", "no rush"])
def test_not_urgent_is_not_an_emergency(env, answer):
    env.add_plumber()
    for text in ("plumber", "burst pipe", answer, "Randburg", "skip"):
        env.post_wa(CUSTOMER, text=text)
    assert "This week" in lead(env)["summary"]
    assert "Emergency" not in lead(env)["summary"]
    confirm = env.wa_text_to(CUSTOMER)[-1]
    assert "stop-valve" not in confirm                  # no flood advice for a relaxed job


def test_a_plain_emergency_is_still_an_emergency(env):
    env.add_plumber()
    for text in ("plumber", "burst pipe", "it's an emergency", "Randburg", "skip"):
        env.post_wa(CUSTOMER, text=text)
    assert "Emergency — right now" in lead(env)["summary"]
    assert "stop-valve" in env.wa_text_to(CUSTOMER)[-1]


def test_an_answer_the_keywords_cannot_read_goes_to_the_model(env, monkeypatch):
    env.add_plumber()
    fake = FakeAnthropic({**NLU_ANSWER, "How urgent is it": {"label": "This week"}})
    monkeypatch.setattr(main, "anthropic_client", fake)
    for text in ("plumber", "burst pipe", "can someone come tomorrow morning", "Randburg", "skip"):
        env.post_wa(CUSTOMER, text=text)
    assert "This week" in lead(env)["summary"]
    assert [u for s, u in fake.calls if s.startswith("You read a WhatsApp reply")] == [
        "can someone come tomorrow morning"]


def test_with_no_model_an_unreadable_urgency_is_asked_again(env):
    for text in ("plumber", "burst pipe", "can someone come tomorrow morning"):
        env.post_wa(CUSTOMER, text=text)
    assert main.conversations[CUSTOMER]["stage"] == "urgency"
    assert "How urgent is it" in env.wa_text_to(CUSTOMER)[-1]


def test_the_model_saying_unclear_asks_again(env, monkeypatch):
    monkeypatch.setattr(main, "anthropic_client",
                        FakeAnthropic({**NLU_ANSWER, "How urgent is it": {"label": "unclear"}}))
    for text in ("plumber", "burst pipe", "hmm not sure"):
        env.post_wa(CUSTOMER, text=text)
    assert main.conversations[CUSTOMER]["stage"] == "urgency"


# ───────────────────────── job type ─────────────────────────


def start_lead(env, problem, suburb="Randburg"):
    for text in ("plumber", problem, "now", suburb, "skip"):
        env.post_wa(CUSTOMER, text=text)


def job_type_calls(fake):
    return [u for s, u in fake.calls if "What's the issue" in s]


def test_a_clear_problem_needs_no_model(env, monkeypatch):
    env.add_plumber()
    fake = FakeAnthropic(NLU_ANSWER)
    monkeypatch.setattr(main, "anthropic_client", fake)
    start_lead(env, "my toilet is leaking")
    assert lead(env)["keyword"] == "leak"                   # was filed as a blocked drain
    assert job_type_calls(fake) == []


def test_numbers_still_work(env):
    env.add_plumber()
    start_lead(env, "3")
    assert lead(env)["keyword"] == "geyser problem"


def test_a_negated_or_mixed_problem_is_read_by_the_model(env, monkeypatch):
    env.add_plumber()
    fake = FakeAnthropic({**NLU_ANSWER, "What's the issue": {"label": "Leak"}})
    monkeypatch.setattr(main, "anthropic_client", fake)
    start_lead(env, "not a geyser problem, it's the tap")
    assert lead(env)["keyword"] == "leak"                   # the old reader filed this as a geyser
    assert job_type_calls(fake) == ["not a geyser problem, it's the tap"]


def test_with_no_model_an_unreadable_problem_is_asked_again(env):
    for text in ("plumber", "the geyser is leaking"):
        env.post_wa(CUSTOMER, text=text)
    assert main.conversations[CUSTOMER]["stage"] == "job_type"
    assert "I didn't catch that" in env.wa_text_to(CUSTOMER)[-1]


@pytest.mark.parametrize("answer", [{"label": "unclear"}, {"label": "Plumbing"}, {"label": None}])
def test_an_unclear_or_invented_model_answer_asks_again(env, monkeypatch, answer):
    monkeypatch.setattr(main, "anthropic_client",
                        FakeAnthropic({**NLU_ANSWER, "What's the issue": answer}))
    for text in ("plumber", "hmm it's complicated"):
        env.post_wa(CUSTOMER, text=text)
    assert main.conversations[CUSTOMER]["stage"] == "job_type"
    assert "I didn't catch that" in env.wa_text_to(CUSTOMER)[-1]


# ───────────────────────── new plumbers wait for approval ─────────────────────────

NEW_PLUMBER = "27835550000"


def join(env, phone=NEW_PLUMBER, areas="Randburg, Sandton"):
    for text in ("join", "Lerato", "7, PIRB123", "1, 2", areas, "1", "YES"):
        env.post_wa(phone, text=text)
    return env.store.find_plumber_by_whatsapp(phone)


def founder_text(env, fragment):
    return [t for t in env.tg_texts() if fragment in t]


def test_a_new_plumber_is_held_and_told_to_wait(env):
    lerato = join(env)
    assert env.store.get_holds(lerato["id"]) == {"pending_approval"}
    welcome = env.wa_text_to(NEW_PLUMBER)[-1]
    assert "approved" in welcome and "Welcome" not in welcome
    assert "reply *take*" not in welcome.lower()               # no "you'll get jobs" promise yet
    alert = founder_text(env, "WAITING FOR APPROVAL")[-1]
    assert f"/approve {str(lerato['id'])[:8]}" in alert and "Held until you approve" in alert


def test_a_pending_plumber_is_not_suggested_for_leads(env):
    lerato = join(env, areas="Randburg")
    env.run_lead_flow(suburb="Randburg")
    sends = [p for m, p in env.tg_calls if m == "sendMessage"]
    buttons = [b["callback_data"] for row in sends[-1]["reply_markup"]["inline_keyboard"] for b in row]
    assert not any(b.startswith("o:") for b in buttons)        # nobody to suggest
    assert not any(b.startswith("m:") for b in buttons)
    assert f"l:L-0001" in buttons                               # "Pick a plumber" is still there
    assert lerato["id"] not in {o["plumber_id"] for o in env.store.offers.values()}


def test_the_founder_can_still_offer_anyway_to_a_pending_plumber(env):
    lerato = join(env, areas="Randburg")
    env.run_lead_flow(suburb="Randburg")
    env.founder_tap(f"o:L-0001:{p8(lerato)}")
    assert "Offer anyway?" in env.tg_texts()[-1]               # an explicit step, never automatic


def test_pending_lists_the_plumber_and_approve_releases_them(env):
    lerato = join(env, areas="Randburg")
    env.founder_says("/pending")
    listing = env.tg_texts()[-1]
    assert "Lerato" in listing and str(lerato["id"])[:8] in listing and "7 years" in listing

    env.founder_says(f"/approve {str(lerato['id'])[:8]}")
    assert env.store.get_holds(lerato["id"]) == set()
    assert any("approved" in t and "Lerato" in t for t in env.wa_text_to(NEW_PLUMBER))
    assert "Approved Lerato" in env.tg_texts()[-1]

    env.founder_says("/pending")
    assert env.tg_texts()[-1] == "No plumbers waiting for approval."

    env.run_lead_flow(suburb="Randburg")                        # now suggested like anyone else
    sends = [p for m, p in env.tg_calls if m == "sendMessage"]
    buttons = [b["callback_data"] for row in sends[-1]["reply_markup"]["inline_keyboard"] for b in row]
    assert f"o:L-0001:{p8(lerato)}" in buttons


def test_approve_needs_a_real_waiting_plumber(env):
    sipho = env.add_plumber()                                   # an existing, active plumber
    env.founder_says("/approve")
    assert "Usage: /approve" in env.tg_texts()[-1]
    env.founder_says("/approve zzzz")
    assert "No single plumber" in env.tg_texts()[-1]
    env.founder_says(f"/approve {p8(sipho)}")
    assert "not waiting for approval" in env.tg_texts()[-1]
    assert env.store.get_holds(sipho["id"]) == set()


def test_only_the_founder_can_approve(env):
    lerato = join(env)
    env.post_tg({"message": {"from": {"id": 123}, "chat": {"id": 111}, "text": f"/approve {str(lerato['id'])[:8]}"}})
    assert env.store.get_holds(lerato["id"]) == {"pending_approval"}


def test_a_pending_plumber_cannot_resume_their_way_in(env):
    lerato = join(env)
    env.post_wa(NEW_PLUMBER, text="RESUME")
    assert env.store.get_holds(lerato["id"]) == {"pending_approval"}
    assert "waiting for approval" in env.wa_text_to(NEW_PLUMBER)[-1]


def test_a_pending_plumber_sees_waiting_in_status_and_when_they_join_again(env):
    join(env)
    env.post_wa(NEW_PLUMBER, text="STATUS")
    assert "Waiting for approval" in env.wa_text_to(NEW_PLUMBER)[-1]
    env.post_wa(NEW_PLUMBER, text="join")
    again = env.wa_text_to(NEW_PLUMBER)[-1]
    assert "approved" in again and "already registered" not in again


def test_existing_plumbers_are_untouched(env):
    sipho = env.add_plumber()
    env.post_wa(PLUMBER_PHONE, text="STATUS")
    assert "Available" in env.wa_text_to(PLUMBER_PHONE)[-1]
    env.run_lead_flow()
    sends = [p for m, p in env.tg_calls if m == "sendMessage"]
    buttons = [b["callback_data"] for row in sends[-1]["reply_markup"]["inline_keyboard"] for b in row]
    assert f"o:L-0001:{p8(sipho)}" in buttons


def test_if_the_hold_cannot_be_set_the_founder_is_told_loudly(env, monkeypatch):
    def refuse(plumber_id, reason):
        raise RuntimeError("violates check constraint plumber_holds_reason_check")
    monkeypatch.setattr(env.store, "add_hold", refuse)
    join(env)
    alert = founder_text(env, "HOLD FAILED")[-1]
    assert "NOT held" in alert and "004_pending_approval.sql" in alert
    assert "Lerato" in alert


def test_if_the_plumber_cannot_be_saved_the_founder_is_told(env, monkeypatch):
    def refuse(row):
        raise RuntimeError("db down")
    monkeypatch.setattr(env.store, "insert_plumber", refuse)
    join(env)
    assert "NOT SAVED" in founder_text(env, "NOT SAVED")[-1]


# ───────────────────────── onboarding: experience and PIRB in plain words ─────────────────────────

def say(env, phone, *texts):
    for text in texts:
        env.post_wa(phone, text=text)


def stage(phone):
    return main.plumber_onboarding[phone]["stage"]


def summary(env, phone):
    return next(t for t in reversed(env.wa_text_to(phone)) if "Please confirm your details" in t)


@pytest.mark.parametrize("answer", ["5 years", "5 yrs", "five years", "5", "10+ years"])
def test_years_in_plain_words_are_accepted_and_pirb_is_asked_next(env, answer):
    say(env, NEW_PLUMBER, "join", "Lerato", answer)
    assert stage(NEW_PLUMBER) == "ob_pirb"
    assert "Are you PIRB registered?" in env.wa_text_to(NEW_PLUMBER)[-1]
    say(env, NEW_PLUMBER, "no")
    assert stage(NEW_PLUMBER) == "ob_services"
    state = main.plumber_onboarding[NEW_PLUMBER]
    assert state["pirb_registered"] is False and state["pirb_number"] is None
    assert state["years_experience"] == {"5 years": 5, "5 yrs": 5, "five years": 5, "5": 5, "10+ years": 10}[answer]


def test_years_and_pirb_in_one_message_skip_the_follow_up(env):
    say(env, NEW_PLUMBER, "join", "Lerato", "five years, PIRB 12345")
    assert stage(NEW_PLUMBER) == "ob_services"
    state = main.plumber_onboarding[NEW_PLUMBER]
    assert (state["years_experience"], state["pirb_registered"], state["pirb_number"]) == (5, True, "PIRB 12345")


def test_the_old_format_still_works(env):
    say(env, NEW_PLUMBER, "join", "Lerato", "3, No PIRB")
    state = main.plumber_onboarding[NEW_PLUMBER]
    assert stage(NEW_PLUMBER) == "ob_services"
    assert (state["years_experience"], state["pirb_registered"]) == (3, False)


def test_an_answer_with_no_years_is_asked_again_with_an_example(env):
    say(env, NEW_PLUMBER, "join", "Lerato", "lots")
    assert stage(NEW_PLUMBER) == "ob_experience"
    assert "I didn't catch the years" in env.wa_text_to(NEW_PLUMBER)[-1]
    say(env, NEW_PLUMBER, "8 years")
    assert stage(NEW_PLUMBER) == "ob_pirb"


def test_an_unreadable_pirb_answer_is_asked_again(env):
    say(env, NEW_PLUMBER, "join", "Lerato", "5 years", "hmm")
    assert stage(NEW_PLUMBER) == "ob_pirb"
    assert "Sorry, I didn't catch that" in env.wa_text_to(NEW_PLUMBER)[-1]
    say(env, NEW_PLUMBER, "PIRB 4411")
    state = main.plumber_onboarding[NEW_PLUMBER]
    assert stage(NEW_PLUMBER) == "ob_services" and state["pirb_number"] == "PIRB 4411"


def test_not_saying_anything_about_pirb_is_no_longer_read_as_not_registered(env):
    say(env, NEW_PLUMBER, "join", "Lerato", "5 years")
    assert "pirb_registered" not in main.plumber_onboarding[NEW_PLUMBER]    # asked, not assumed


def test_registered_without_a_number_is_shown_honestly(env):
    say(env, NEW_PLUMBER, "join", "Lerato", "5, yes", "1, 2", "Randburg", "1")
    assert "Says registered, no number given" in summary(env, NEW_PLUMBER)
    assert "None" not in summary(env, NEW_PLUMBER)
    say(env, NEW_PLUMBER, "YES")
    alert = [t for t in env.tg_texts() if "WAITING FOR APPROVAL" in t][-1]
    assert "PIRB: Says registered, no number given" in alert
    assert env.store.find_plumber_by_whatsapp(NEW_PLUMBER)["pirb_registered"] is True


def test_the_summary_shows_what_was_read_so_a_misread_can_be_declined(env):
    say(env, NEW_PLUMBER, "join", "Lerato", "twelve years, PIRB 99812", "1, 2", "Randburg", "1")
    text = summary(env, NEW_PLUMBER)
    assert "12 years" in text and "PIRB 99812" in text
    say(env, NEW_PLUMBER, "NO")
    assert env.store.find_plumber_by_whatsapp(NEW_PLUMBER) is None


def test_a_full_signup_with_plain_answers_saves_and_holds_the_plumber(env):
    say(env, NEW_PLUMBER, "join", "Lerato", "7 years", "no", "1, 2", "Rivonia Ext 3, Sandton", "1", "YES")
    saved = env.store.find_plumber_by_whatsapp(NEW_PLUMBER)
    assert (saved["experience_years"], saved["pirb_registered"]) == (7, False)
    assert saved["areas"] == ["rivonia", "sandton"]
    assert env.store.get_holds(saved["id"]) == {"pending_approval"}


# ───────────────────────── trial leads ─────────────────────────

def pending_plumber(env, trial_stage=None, phone=PLUMBER_PHONE, name="Thabo"):
    p = env.add_plumber(name=name, phone=phone)
    env.store.plumbers[p["id"]]["trial_stage"] = trial_stage
    env.store.add_hold(p["id"], "pending_approval")
    return p


def test_approving_a_new_plumber_gives_five_trial_leads_and_says_so(env):
    lerato = join(env, areas="Randburg")
    assert env.store.plumbers[lerato["id"]].get("trial_stage") is None      # none until approved
    env.founder_says(f"/approve {str(lerato['id'])[:8]}")
    assert env.store.plumbers[lerato["id"]]["trial_stage"] == 5
    assert any("You have 5 trial leads" in t and "no fee" in t for t in env.wa_text_to(NEW_PLUMBER))
    assert "5 trial leads" in env.tg_texts()[-1]


def test_approving_never_resets_a_plumbers_trial_leads(env):
    some = pending_plumber(env, trial_stage=2)
    env.founder_says(f"/approve {p8(some)}")
    assert env.store.plumbers[some["id"]]["trial_stage"] == 2
    assert "2 trial leads" in env.tg_texts()[-1]

    used = pending_plumber(env, trial_stage=0, phone="27821112222", name="Used")
    env.founder_says(f"/approve {p8(used)}")
    assert env.store.plumbers[used["id"]]["trial_stage"] == 0
    assert "No trial leads" in env.tg_texts()[-1]
    assert not any("trial lead" in t for t in env.wa_text_to("27821112222"))


def test_one_trial_lead_left_reads_naturally(env):
    one = pending_plumber(env, trial_stage=1)
    env.founder_says(f"/approve {p8(one)}")
    assert any("You have 1 trial lead:" in t for t in env.wa_text_to(PLUMBER_PHONE))


def test_status_shows_trial_leads_left_only_when_there_are_some(env):
    env.add_plumber()
    env.store.plumbers[next(iter(env.store.plumbers))]["trial_stage"] = 3
    env.post_wa(PLUMBER_PHONE, text="STATUS")
    assert "Trial leads left: 3" in env.wa_text_to(PLUMBER_PHONE)[-1]
    env.store.plumbers[next(iter(env.store.plumbers))]["trial_stage"] = 0
    env.post_wa(PLUMBER_PHONE, text="STATUS")
    assert "Trial leads left" not in env.wa_text_to(PLUMBER_PHONE)[-1]


def test_a_trial_lead_is_free_from_offer_to_job(env):
    lerato = join(env, areas="Randburg")
    env.founder_says(f"/approve {str(lerato['id'])[:8]}")
    env.run_lead_flow(suburb="Randburg")
    env.founder_tap(f"o:L-0001:{p8(lerato)}")
    offer = env.wa_text_to(NEW_PLUMBER)[-1]
    assert "Trial lead · no fee" in offer and "R220" not in offer
    assert env.store.plumbers[lerato["id"]]["trial_stage"] == 5         # offered, not yet used

    env.post_wa(NEW_PLUMBER, text="take")
    job = env.store.get_job_by_lead("L-0001")
    assert job["fee_cents"] == 0 and "trial_lead" in job["flags"]
    assert env.store.plumbers[lerato["id"]]["trial_stage"] == 4          # used when they took it
    took = env.wa_text_to(NEW_PLUMBER)[-1]
    assert "You've got it." in took and "trial lead, so no fee" in took and "4 trial leads left" in took


def test_a_plumber_without_trial_leads_still_sees_the_normal_fee(env):
    sipho = env.add_plumber()                                            # no trial_stage
    env.run_lead_flow(suburb="Randburg")
    env.founder_tap(f"o:L-0001:{p8(sipho)}")
    offer = env.wa_text_to(PLUMBER_PHONE)[-1]
    assert "R220 due after the job is done" in offer and "Trial lead" not in offer
    env.post_wa(PLUMBER_PHONE, text="take")
    assert env.store.get_job_by_lead("L-0001")["fee_cents"] == 22000
    assert "trial lead" not in env.wa_text_to(PLUMBER_PHONE)[-1]


def test_only_the_plumber_who_takes_it_uses_a_trial_lead(env):
    a = env.add_plumber(name="Ayanda", phone="27821110001"); env.store.plumbers[a["id"]]["trial_stage"] = 3
    b = env.add_plumber(name="Bheki", phone="27821110002"); env.store.plumbers[b["id"]]["trial_stage"] = 3
    env.run_lead_flow(suburb="Randburg")
    env.founder_tap(f"o:L-0001:{p8(a)}")
    env.founder_tap(f"o:L-0001:{p8(b)}")
    env.post_wa("27821110001", text="take")
    assert env.store.plumbers[a["id"]]["trial_stage"] == 2
    assert env.store.plumbers[b["id"]]["trial_stage"] == 3               # offered, lost the race, kept theirs


# ───────────────────────── onboarding: the name ─────────────────────────

def test_the_first_question_is_the_name(env):
    say(env, NEW_PLUMBER, "join")
    assert stage(NEW_PLUMBER) == "ob_name"
    assert "What's your first name?" in env.wa_text_to(NEW_PLUMBER)[-1]


@pytest.mark.parametrize("answer,saved", [
    ("Sipho", "Sipho"),
    ("my name is Sipho Dlamini", "Sipho"),
    ("I'm Sipho", "Sipho"),
    ("This is Lerato from Soweto", "Lerato"),
])
def test_the_name_is_read_from_a_normal_answer_and_echoed_back(env, answer, saved):
    say(env, NEW_PLUMBER, "join", answer)
    assert main.plumber_onboarding[NEW_PLUMBER]["first_name"] == saved
    assert stage(NEW_PLUMBER) == "ob_experience"
    assert env.wa_text_to(NEW_PLUMBER)[-1].startswith(f"Thanks, {saved}.")        # a wrong read is visible at once


@pytest.mark.parametrize("answer", ["hello", "yes", "ok", "123", "x"])
def test_something_that_is_not_a_name_is_asked_again(env, answer):
    say(env, NEW_PLUMBER, "join", answer)
    assert stage(NEW_PLUMBER) == "ob_name"
    assert "I didn't catch your name" in env.wa_text_to(NEW_PLUMBER)[-1]
    say(env, NEW_PLUMBER, "Sipho")
    assert stage(NEW_PLUMBER) == "ob_experience"


def test_the_saved_plumber_has_the_name_they_gave(env):
    say(env, NEW_PLUMBER, "join", "my name is Lerato", "5 years", "no", "1, 2", "Randburg", "1", "YES")
    saved = env.store.find_plumber_by_whatsapp(NEW_PLUMBER)
    assert saved["first_name"] == "Lerato"
    assert any("Lerato" in t and "WAITING FOR APPROVAL" in t for t in env.tg_texts())


# ───────────────────────── /health ─────────────────────────

def test_health_is_ok_when_everything_works(env):
    r = env.client.get("/health")
    assert r.status_code == 200
    assert r.json() == {"status": "ok", "service": "plumberflow", "services": "ready", "database": "ok",
                        "scheduler": "running", "admin": "not configured"}


def test_health_says_degraded_with_503_when_the_database_does_not_answer(env, monkeypatch):
    monkeypatch.setattr(env.store, "ping", lambda: False)
    r = env.client.get("/health")
    assert r.status_code == 503 and r.json()["status"] == "degraded" and r.json()["database"] == "unreachable"


def test_health_survives_the_database_raising(env, monkeypatch):
    def down():
        raise RuntimeError("connection refused")
    monkeypatch.setattr(env.store, "ping", down)
    r = env.client.get("/health")
    assert r.status_code == 503 and r.json()["database"] == "unreachable"


def test_health_does_not_leak_anything_secret(env):
    assert "postgres" not in env.client.get("/health").text.lower()


def test_starting_with_a_bad_secret_stops_with_the_whole_report(monkeypatch):
    import services
    from config_check import ConfigError
    for k in list(services.os.environ):
        if k in ("DATABASE_URL", "FOUNDER_USER_ID", "WHATSAPP_TOKEN", "WA_TOKEN", "PHONE_NUMBER_ID", "WA_PHONE_NUMBER_ID",
                 "TELEGRAM_BOT_TOKEN", "TELEGRAM_TOKEN", "TELEGRAM_CHAT_ID", "FOUNDER_CHAT_ID"):
            monkeypatch.delenv(k)
    monkeypatch.setenv("DATABASE_URL", "<psql 'postgresql://u:topsecret@h/db'>")
    monkeypatch.setenv("FOUNDER_USER_ID", "< 123 --stage")
    monkeypatch.setattr(services, "AppStore", lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not connect")))
    with pytest.raises(ConfigError) as err:
        services.build_services()
    text = str(err.value)
    assert "DATABASE_URL" in text and "FOUNDER_USER_ID" in text and "topsecret" not in text
