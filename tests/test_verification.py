import inspect
import re
from datetime import datetime, timedelta

import pytest

from verification import Outcome, VerificationService, resolve
from verification import templates as T
from verification.parsing import (
    is_wrong, parse_a9_reply, parse_amount_cents, parse_choice, parse_intent,
)
from verification.service import SAST


# ───────────────────────── Fakes ─────────────────────────

class FakeMessenger:
    def __init__(self):
        self.plumber, self.customer, self.buttons, self.founder = [], [], [], []

    def send_plumber(self, plumber, text):
        self.plumber.append((plumber["first_name"], text))

    def send_customer(self, phone, text):
        self.customer.append((phone, text))

    def send_customer_buttons(self, phone, text, buttons, job_id):
        self.buttons.append((phone, text, buttons, job_id))

    def notify_founder(self, text):
        self.founder.append(text)

    def plumber_texts(self):
        return [t for _, t in self.plumber]


class Clock:
    def __init__(self, dt):
        self.dt = dt

    def __call__(self):
        return self.dt

    def set(self, dt):
        self.dt = dt


def sast(y, m, d, h=10, mi=0):
    return datetime(y, m, d, h, mi, tzinfo=SAST)


MON = sast(2026, 10, 5)  # Monday 5 Oct 2026


@pytest.fixture
def env(store):
    msg, clock = FakeMessenger(), Clock(MON)
    svc = VerificationService(store, msg, clock=clock)
    sipho = store.add_plumber("Sipho")
    return store, msg, clock, svc, sipho


def accept(svc, plumber, n=1, phone="0821111111", name="Sarah", keyword="geyser"):
    return svc.register_acceptance(
        lead_id=f"lead-{phone}-{n}-{svc.now().isoformat()}", plumber_id=plumber["id"],
        customer_phone=phone, customer_name=name, suburb="Fourways", keyword=keyword)


# ───────────────────────── Resolution matrix ─────────────────────────

@pytest.mark.parametrize("claim,resp,age,outcome,flag", [
    ("done", "yes", 0, Outcome.CHARGE, None),
    ("done", "off_price", 0, Outcome.CHARGE, "price_mismatch"),
    ("done", "never_came", 0, Outcome.DISPUTED, "done_but_never_came"),
    ("done", "off", 0, Outcome.DISPUTED, "customer_off_unclassified"),
    ("done", None, 0, Outcome.CHARGE, None),          # silence never blocks a charge
    ("no_go", "yes", 0, Outcome.NO_CHARGE, "no_go_but_customer_confirms"),
    ("no_go", "off_price", 0, Outcome.NO_CHARGE, "no_go_but_customer_confirms"),
    ("no_go", None, 0, Outcome.NO_CHARGE, None),
    ("no_go", "never_came", 0, Outcome.NO_CHARGE, None),
    (None, "yes", 3.9, Outcome.HOLD, None),
    (None, "yes", 4.0, Outcome.CHARGE, None),
    (None, "never_came", 1, Outcome.NO_CHARGE, None),
    (None, None, 4.9, Outcome.HOLD, None),
    (None, None, 5.0, Outcome.UNRESOLVED, None),
    (None, "not_sorted", 5.0, Outcome.UNRESOLVED, None),
])
def test_resolution_matrix(claim, resp, age, outcome, flag):
    r = resolve(claim, resp, age)
    assert r.outcome is outcome
    assert (flag in r.flags) if flag else not r.flags


def test_customer_silence_alone_never_charges():
    for age in (0, 1, 4, 10, 100):
        assert resolve(None, None, age).outcome in (Outcome.HOLD, Outcome.UNRESOLVED)


# ───────────────────────── Parsing ─────────────────────────

def test_parsing():
    assert parse_intent("done") == "done"
    assert parse_intent("Done R2400") == "done"
    assert parse_intent("no go") == "no_go"
    assert parse_intent("nogo") == "no_go"
    assert parse_intent("hello") is None
    assert parse_amount_cents("R2400") == 240000
    assert parse_amount_cents("2 400") == 240000
    assert parse_amount_cents("2,400") == 240000
    assert parse_amount_cents("r2400.50") == 240050
    assert parse_amount_cents("done, 3 hours") is None
    assert parse_amount_cents("nothing") is None
    assert parse_choice(" 2 ") == 2 and parse_choice("2 geysers") is None
    assert parse_a9_reply("1 done R2400, 2 no go, 3 done") == [
        (1, "done", 240000), (2, "no_go", None), (3, "done", None)]
    assert is_wrong("that's wrong") and not is_wrong("ok thanks")


# ───────────────────────── Voice lint ─────────────────────────

BANNED = ["trusted", "vetted", "valued", "welcome to", "hi there", "excited", "premium"]


def test_voice_lint():
    samples = []
    for name, fn in inspect.getmembers(T, inspect.isfunction):
        if name.startswith("_") or name == "rand":
            continue
        args = []
        for p in inspect.signature(fn).parameters.values():
            ann = str(p.annotation)
            if "list" in ann:
                args.append([(1, "Fourways geyser", "Monday")] if "tuple" in ann else ["Fourways geyser · Monday · R220"])
            elif "int" in ann:
                args.append(240000 if "cents" in p.name else 3)
            elif p.name == "restored":
                args.append(False)
            else:
                args.append("x")
        samples.append((name, fn(*args)))
    samples += [("B1_BUTTONS", " ".join(label for _, label in T.B1_BUTTONS))]
    assert len(samples) > 20
    emoji = re.compile("[\U0001F300-\U0001FAFF☀-➿]")
    for name, text in samples:
        assert "!" not in text, f"{name}: exclamation mark"
        assert not emoji.search(text), f"{name}: emoji"
        low = text.lower()
        for b in BANNED:
            assert b not in low, f"{name}: banned phrase {b!r}"


# ───────────────────────── Flows ─────────────────────────

def test_happy_path_done_confirm_invoice_paid(env):
    store, msg, clock, svc, sipho = env
    job = accept(svc, sipho)

    assert svc.on_plumber_message(sipho["id"], "done")
    assert msg.plumber_texts()[-1] == T.a4_ask_value()
    assert svc.on_plumber_message(sipho["id"], "R2 400")
    j = store.get_job(job["id"])
    assert j["plumber_claim"] == "done" and j["job_value_cents"] == 240000
    assert j["status"] == "charged"                      # plumber claim stands unopposed
    assert "R2,400" in msg.plumber_texts()[-1] and "R220" in msg.plumber_texts()[-1]

    clock.set(MON + timedelta(hours=1))
    svc.sweep_jobs()
    assert not msg.buttons                               # B1 not due before 2h

    clock.set(MON + timedelta(hours=2, minutes=1))       # Monday 12:01
    svc.sweep_jobs()
    assert len(msg.buttons) == 1
    assert "R2,400" in msg.buttons[0][1] and msg.buttons[0][2] == T.B1_BUTTONS
    svc.sweep_jobs()
    assert len(msg.buttons) == 1                         # idempotent

    svc.on_customer_button(job["id"], "yes")
    assert msg.customer[-1][1].startswith("Good. Thanks Sarah")

    clock.set(sast(2026, 10, 9, 17, 5))                  # Friday 17:05
    svc.sweep_invoices()
    inv = store.list_invoices(plumber_id=sipho["id"])[0]
    assert inv["total_cents"] == 22000 and inv["status"] == "sent"
    assert "R220 due" in msg.plumber_texts()[-1]
    svc.sweep_invoices()
    assert len(store.list_invoices(plumber_id=sipho["id"])) == 1   # one invoice per week

    svc.mark_invoice_paid(inv["id"])
    assert msg.plumber_texts()[-1] == "Got it. R220 cleared. Thanks."


def test_done_with_amount_in_one_message(env):
    store, msg, clock, svc, sipho = env
    job = accept(svc, sipho)
    svc.on_plumber_message(sipho["id"], "done R1800")
    assert store.get_job(job["id"])["job_value_cents"] == 180000


def test_value_retry_then_give_up(env):
    store, msg, clock, svc, sipho = env
    accept(svc, sipho)
    svc.on_plumber_message(sipho["id"], "done")
    assert svc.on_plumber_message(sipho["id"], "lots")
    assert msg.plumber_texts()[-1] == T.a4_value_retry()
    assert svc.on_plumber_message(sipho["id"], "no idea") is False
    assert store.get_plumber_state(sipho["id"]) is None


def test_no_open_jobs(env):
    store, msg, clock, svc, sipho = env
    assert svc.on_plumber_message(sipho["id"], "done")
    assert msg.plumber_texts()[-1] == T.no_open_jobs()


def test_multiple_open_jobs_ask_which(env):
    store, msg, clock, svc, sipho = env
    a = accept(svc, sipho, phone="0821111111")
    clock.set(MON + timedelta(hours=1))
    b = accept(svc, sipho, phone="0822222222", name="Thabo", keyword="drain")
    svc.on_plumber_message(sipho["id"], "done")
    assert msg.plumber_texts()[-1].startswith("Which one?")
    svc.on_plumber_message(sipho["id"], "2")
    assert msg.plumber_texts()[-1] == T.a4_ask_value()
    svc.on_plumber_message(sipho["id"], "950")
    assert store.get_job(b["id"])["job_value_cents"] == 95000
    assert store.get_job(a["id"])["plumber_claim"] is None


def test_no_go_but_customer_says_job_happened_flags_and_never_charges(env):
    store, msg, clock, svc, sipho = env
    job = accept(svc, sipho)
    svc.on_plumber_message(sipho["id"], "no go")
    assert store.get_job(job["id"])["status"] == "no_charge"
    assert msg.plumber_texts()[-1].startswith("Noted. Nothing owed.")
    svc.on_plumber_message(sipho["id"], "customer wasn't home")
    assert store.get_job(job["id"])["no_go_reason"] == "customer wasn't home"

    clock.set(MON + timedelta(days=1, hours=1))          # Tuesday 11:00: customer is asked
    svc.sweep_jobs()
    assert any("did Sipho get the geyser sorted" in t for _, t in msg.customer)

    assert svc.on_customer_reply("0821111111", "sorted")
    after = store.get_job(job["id"])
    assert after["status"] == "no_charge"                # never charged
    assert "no_go_but_customer_confirms" in after["flags"]
    assert sum("FRAUD SIGNAL" in f for f in msg.founder) == 1
    assert "Count for this plumber: 1" in msg.founder[-1]


def test_no_go_and_customer_agrees_is_clean(env):
    store, msg, clock, svc, sipho = env
    job = accept(svc, sipho)
    svc.on_plumber_message(sipho["id"], "no go")
    clock.set(MON + timedelta(days=1, hours=1))
    svc.sweep_jobs()
    svc.on_customer_reply("0821111111", "never_came")
    assert store.get_job(job["id"])["flags"] == []
    assert not msg.founder or all("FRAUD" not in f for f in msg.founder)


def test_plumber_silent_customer_confirms_day4_notice_then_charge(env):
    store, msg, clock, svc, sipho = env
    job = accept(svc, sipho)

    clock.set(MON + timedelta(days=1, minutes=30))       # Tuesday 10:30
    svc.sweep_jobs()
    assert any("still open on my side" in t for t in msg.plumber_texts())     # A3
    assert any("did Sipho get the geyser sorted" in t for _, t in msg.customer)  # B2
    svc.sweep_jobs()
    assert sum("still open on my side" in t for t in msg.plumber_texts()) == 1

    assert svc.on_customer_reply("0821111111", "sorted")
    assert store.get_job(job["id"])["status"] == "accepted"   # held: day 4 not reached

    clock.set(MON + timedelta(days=4))                   # Friday 10:00
    svc.sweep_jobs()
    j = store.get_job(job["id"])
    assert j["status"] == "charged" and j["a7_sent_at"]
    assert "confirmed the geyser was sorted on Monday" in msg.plumber_texts()[-1]
    assert store.get_plumber_state(sipho["id"])["step"] == "a7_reply"


def test_plumber_contests_a7_charge(env):
    store, msg, clock, svc, sipho = env
    job = accept(svc, sipho)
    clock.set(MON + timedelta(days=1, minutes=30))
    svc.sweep_jobs()
    svc.on_customer_reply("0821111111", "sorted")
    clock.set(MON + timedelta(days=4))
    svc.sweep_jobs()
    assert svc.on_plumber_message(sipho["id"], "that's wrong, I never went")
    j = store.get_job(job["id"])
    assert j["status"] == "disputed" and j["charged_at"] is None
    assert msg.plumber_texts()[-1] == T.a7_pulled()
    assert any("CONTESTED CHARGE" in f for f in msg.founder)


def test_done_but_customer_says_never_came_is_disputed_not_charged(env):
    store, msg, clock, svc, sipho = env
    job = accept(svc, sipho)
    svc.on_plumber_message(sipho["id"], "done 2400")
    assert store.get_job(job["id"])["status"] == "charged"   # claim stands until contradicted

    clock.set(MON + timedelta(hours=3))                      # B1 goes out after 2h
    svc.sweep_jobs()
    assert len(msg.buttons) == 1
    assert svc.on_customer_reply("0821111111", "never_came")
    j = store.get_job(job["id"])
    assert j["status"] == "disputed" and j["charged_at"] is None
    assert "done_but_never_came" in j["flags"]
    assert any("HIGH SIGNAL" in f for f in msg.founder)

    clock.set(sast(2026, 10, 9, 17, 5))                      # disputed jobs never reach an invoice
    svc.sweep_invoices()
    assert store.list_invoices(plumber_id=sipho["id"])[0]["total_cents"] == 0

    svc.admin_resolve(job["id"], "no_charge", "no visit happened")
    assert store.get_job(job["id"])["status"] == "no_charge"


def test_price_mismatch_still_charges_but_flags(env):
    store, msg, clock, svc, sipho = env
    job = accept(svc, sipho)
    svc.on_plumber_message(sipho["id"], "done 1500")
    clock.set(MON + timedelta(hours=3))
    svc.sweep_jobs()
    svc.on_customer_reply("0821111111", "off_price", actual_value_cents=250000)
    j = store.get_job(job["id"])
    assert j["status"] == "charged" and "price_mismatch" in j["flags"]
    assert "R2,500" in msg.customer[-1][1] and "R1,500" in msg.customer[-1][1]


def test_both_silent_unresolved_then_pattern_pause_and_release(env):
    store, msg, clock, svc, sipho = env
    jobs = []
    for i in range(3):
        jobs.append(accept(svc, sipho, phone=f"082000000{i}"))
    clock.set(MON + timedelta(days=5))                   # Saturday 10:00
    svc.sweep_jobs()
    assert all(store.get_job(j["id"])["status"] == "unresolved" for j in jobs)
    assert not any("charged" in j["status"] for j in store.list_jobs())
    svc.sweep_patterns()
    assert svc.is_leads_paused(sipho["id"])
    assert "3 jobs open on my side" in msg.plumber_texts()[-1]
    svc.sweep_patterns()
    assert sum("jobs open on my side" in t for t in msg.plumber_texts()) == 1

    assert svc.on_plumber_message(sipho["id"], "1 done R2400, 2 no go, 3 no go")
    assert msg.plumber_texts()[-1] == T.a9_progress(3, 0)
    assert not svc.is_leads_paused(sipho["id"])
    statuses = sorted(store.get_job(j["id"])["status"] for j in jobs)
    assert statuses == ["charged", "no_charge", "no_charge"]


def test_overdue_invoice_reminder_pause_and_restore(env):
    store, msg, clock, svc, sipho = env
    accept(svc, sipho)
    svc.on_plumber_message(sipho["id"], "done 2400")
    clock.set(sast(2026, 10, 9, 17, 30))
    svc.sweep_invoices()
    inv = store.list_invoices(plumber_id=sipho["id"])[0]

    clock.set(sast(2026, 10, 12, 20))                    # Monday evening: not yet due-passed
    svc.sweep_invoices()
    assert not any("outstanding" in t for t in msg.plumber_texts())

    clock.set(sast(2026, 10, 13, 9, 30))                 # Tuesday after the due Monday (12 Oct)
    svc.sweep_invoices()
    assert "R220 from last week still outstanding" in msg.plumber_texts()[-1]
    assert not svc.is_leads_paused(sipho["id"])

    clock.set(sast(2026, 10, 15, 9, 30))                 # Thursday
    svc.sweep_invoices()
    assert svc.is_leads_paused(sipho["id"])
    assert msg.plumber_texts()[-1].startswith("Leads paused, Sipho")

    svc.mark_invoice_paid(inv["id"])
    assert not svc.is_leads_paused(sipho["id"])
    assert msg.plumber_texts()[-1] == "Got it. R220 cleared. Leads are back on."


def test_zero_week_message_once(env):
    store, msg, clock, svc, sipho = env
    clock.set(sast(2026, 10, 9, 17, 5))
    svc.sweep_invoices()
    svc.sweep_invoices()
    zero = [t for t in msg.plumber_texts() if "No completed jobs" in t]
    assert len(zero) == 1


def test_quiet_hours_respected(env):
    store, msg, clock, svc, sipho = env
    accept(svc, sipho)
    clock.set(sast(2026, 10, 6, 22, 30))                 # Tuesday 22:30, age > 24h but outside windows
    svc.sweep_jobs()
    assert not msg.plumber and not msg.customer
    clock.set(sast(2026, 10, 7, 10, 30))
    svc.sweep_jobs()
    assert msg.plumber and msg.customer


def test_credit_transition_after_three_settled_jobs(store):
    msg, clock = FakeMessenger(), Clock(MON)
    svc = VerificationService(store, msg, clock=clock, credit_link="https://pay.example/load")
    p = store.add_plumber("Sipho")
    for week in range(3):
        base = MON + timedelta(days=7 * week)
        clock.set(base)
        accept(svc, p, phone=f"08200000{week}")
        svc.on_plumber_message(p["id"], "done 2400")
        clock.set(base + timedelta(days=4, hours=7, minutes=5))
        svc.sweep_invoices()
        inv = [i for i in store.list_invoices(plumber_id=p["id"], statuses=("sent",))][0]
        svc.mark_invoice_paid(inv["id"])
    assert sum("moves to credit" in t for t in msg.plumber_texts()) == 1
    assert any("https://pay.example/load" in t for t in msg.plumber_texts())


def test_register_acceptance_idempotent(env):
    store, msg, clock, svc, sipho = env
    a = svc.register_acceptance(lead_id="L1", plumber_id=sipho["id"], customer_phone="0821111111",
                                customer_name="Sarah", suburb="Fourways", keyword="geyser")
    b = svc.register_acceptance(lead_id="L1", plumber_id=sipho["id"], customer_phone="0821111111",
                                customer_name="Sarah", suburb="Fourways", keyword="geyser")
    assert a["id"] == b["id"] and len(store.list_jobs()) == 1
