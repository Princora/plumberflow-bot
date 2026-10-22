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
    assert store.list_invoices(plumber_id=sipho["id"]) == []         # nothing owed, nothing sent

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


def test_a_week_with_nothing_owed_sends_nothing(env):
    store, msg, clock, svc, sipho = env
    clock.set(sast(2026, 10, 9, 17, 5))
    svc.sweep_invoices()
    assert store.list_invoices(plumber_id=sipho["id"]) == []
    assert msg.plumber_texts() == []


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


# ───────────────────────── Reading plumber replies ─────────────────────────

from verification.interpret import ClaudeReplyInterpreter
from verification.parsing import classify_agreement, is_bare_amount, is_clean_done_message


@pytest.mark.parametrize("text,cents", [
    ("done R1800", 180000),
    ("done 120 min job R1800", 180000),          # was R120
    ("done 2024 R1800", 180000),                 # was R2,024
    ("done, 3 geysers R4500", 450000),           # was None
    ("Done 15 Oct R2400", 240000),               # was None
    ("done R2k", 200000),
    ("done 1800", 180000),
    ("Job done. Charged R 2 400 incl. R350 callout", None),   # two prices: ask, don't guess
    ("done 1800 incl 350 callout", None),
    ("done, took 3 hours", None),
    ("done 0821234567", None),                   # a phone number
])
def test_amount_parsing_does_not_guess(text, cents):
    assert parse_amount_cents(text) == cents


@pytest.mark.parametrize("text,verdict", [
    ("that's wrong", "disputes"),
    ("wrong", "disputes"),
    ("that is not right", "disputes"),
    ("I didn't do that job", "disputes"),
    ("no that's right", "agrees"),               # was read as a dispute
    ("yes correct, no problem", "agrees"),       # was read as a dispute
    ("no dispute, it's correct", "agrees"),      # was read as a dispute
    ("Nope all good", "agrees"),                 # was read as a dispute
    ("yebo", "agrees"),
    ("no", "unclear"),
    ("hmm let me check", "unclear"),
])
def test_charge_reply_classification(text, verdict):
    assert classify_agreement(text) == verdict


def test_bare_no_can_be_told_it_means_disputes():
    assert classify_agreement("no", bare_no="disputes") == "disputes"
    assert classify_agreement("nope.", bare_no="disputes") == "disputes"


def test_clean_message_shapes():
    assert is_clean_done_message("done R2400") and is_clean_done_message("Done, 2 400")
    assert not is_clean_done_message("done 120 min job R1800")
    assert is_bare_amount("R2400") and is_bare_amount("2 400") and not is_bare_amount("about 2400")


class FakeInterpreter:
    def __init__(self, agreement=None, value=None):
        self._agreement, self._value = agreement, value
        self.agreement_calls, self.value_calls = [], []

    def agreement(self, text, question):
        self.agreement_calls.append(text)
        return self._agreement

    def job_value(self, text):
        self.value_calls.append(text)
        return self._value


def with_interpreter(env, interpreter):
    store, msg, clock, svc, sipho = env
    svc.interpreter = interpreter
    return store, msg, clock, svc, sipho


def test_wordy_done_message_is_confirmed_before_it_is_logged(env):
    store, msg, clock, svc, sipho = env
    job = accept(svc, sipho)
    svc.on_plumber_message(sipho["id"], "done 120 min job R1800")
    assert store.get_job(job["id"])["job_value_cents"] in (None, 0)
    assert msg.plumber_texts()[-1] == T.a4_confirm_value(180000)
    assert svc.on_plumber_message(sipho["id"], "yes")
    assert store.get_job(job["id"])["job_value_cents"] == 180000


def test_confirmation_correction_and_no(env):
    store, msg, clock, svc, sipho = env
    job = accept(svc, sipho)
    svc.on_plumber_message(sipho["id"], "done, 3 hours work and R1800 total R350 callout")
    assert msg.plumber_texts()[-1] == T.a4_ask_value()      # two prices: asked, not guessed
    svc.on_plumber_message(sipho["id"], "R1800 plus R350 callout")
    assert msg.plumber_texts()[-1] == T.a4_value_retry()
    svc.on_plumber_message(sipho["id"], "2150")
    assert store.get_job(job["id"])["job_value_cents"] == 215000   # a bare amount is explicit

    job2 = accept(svc, sipho, n=2, phone="0822222222", name="Thabo")
    svc.on_plumber_message(sipho["id"], "done 120 min job R1800")
    svc.on_plumber_message(sipho["id"], "no")
    assert msg.plumber_texts()[-1] == T.a4_ask_value()
    assert store.get_job(job2["id"])["job_value_cents"] in (None, 0)
    svc.on_plumber_message(sipho["id"], "R1900")
    assert store.get_job(job2["id"])["job_value_cents"] == 190000


def test_confirmation_with_different_wordy_amount_asks_again(env):
    store, msg, clock, svc, sipho = env
    job = accept(svc, sipho)
    svc.on_plumber_message(sipho["id"], "done 120 min job R1800")
    svc.on_plumber_message(sipho["id"], "no it was R2000 cash")
    assert msg.plumber_texts()[-1] == T.a4_confirm_value(200000)
    assert store.get_job(job["id"])["job_value_cents"] in (None, 0)


def test_unreadable_confirmation_logs_nothing(env):
    store, msg, clock, svc, sipho = env
    job = accept(svc, sipho)
    svc.on_plumber_message(sipho["id"], "done 120 min job R1800")
    assert svc.on_plumber_message(sipho["id"], "hmm")
    assert svc.on_plumber_message(sipho["id"], "what")  is False
    assert store.get_plumber_state(sipho["id"]) is None
    assert store.get_job(job["id"])["job_value_cents"] in (None, 0)


def test_model_reads_a_messy_amount_but_the_plumber_confirms_it(env):
    store, msg, clock, svc, sipho = with_interpreter(env, FakeInterpreter(value=240000))
    job = accept(svc, sipho)
    svc.on_plumber_message(sipho["id"], "done, two thousand four hundred")
    assert msg.plumber_texts()[-1] == T.a4_confirm_value(240000)
    assert store.get_job(job["id"])["job_value_cents"] in (None, 0)   # not logged yet
    svc.on_plumber_message(sipho["id"], "yes")
    assert store.get_job(job["id"])["job_value_cents"] == 240000


def test_without_a_model_a_messy_amount_just_asks(env):
    store, msg, clock, svc, sipho = env
    accept(svc, sipho)
    svc.on_plumber_message(sipho["id"], "done, two thousand four hundred")
    assert msg.plumber_texts()[-1] == T.a4_ask_value()


def test_model_is_not_called_when_strict_parsing_is_sure(env):
    fake = FakeInterpreter(value=999900, agreement="disputes")
    store, msg, clock, svc, sipho = with_interpreter(env, fake)
    job = accept(svc, sipho)
    svc.on_plumber_message(sipho["id"], "done R1800")
    assert store.get_job(job["id"])["job_value_cents"] == 180000
    assert fake.value_calls == [] and fake.agreement_calls == []


def to_a7(env):
    store, msg, clock, svc, sipho = env
    job = accept(svc, sipho)
    clock.set(MON + timedelta(days=1, minutes=30))
    svc.sweep_jobs()
    svc.on_customer_reply("0821111111", "sorted")
    clock.set(MON + timedelta(days=4))
    svc.sweep_jobs()
    assert store.get_plumber_state(sipho["id"])["step"] == "a7_reply"
    return job


@pytest.mark.parametrize("reply", ["no that's right", "yes correct, no problem", "Nope all good"])
def test_agreeing_with_the_charge_does_not_pull_it(env, reply):
    store, msg, clock, svc, sipho = env
    job = to_a7(env)
    assert svc.on_plumber_message(sipho["id"], reply)
    assert store.get_job(job["id"])["status"] != "disputed"
    assert not any("CONTESTED" in f for f in msg.founder)
    assert store.get_plumber_state(sipho["id"]) is None


def test_bare_no_after_a7_asks_instead_of_guessing(env):
    store, msg, clock, svc, sipho = env
    job = to_a7(env)
    assert svc.on_plumber_message(sipho["id"], "no")
    assert msg.plumber_texts()[-1] == T.a7_clarify()
    assert store.get_job(job["id"])["status"] != "disputed"
    assert svc.on_plumber_message(sipho["id"], "wrong")
    assert store.get_job(job["id"])["status"] == "disputed"


def test_unreadable_reply_to_a7_leaves_the_charge_and_flags_a_human(env):
    store, msg, clock, svc, sipho = env
    job = to_a7(env)
    svc.on_plumber_message(sipho["id"], "hmm")
    svc.on_plumber_message(sipho["id"], "let me think")
    assert store.get_job(job["id"])["status"] != "disputed"
    assert any("UNCLEAR REPLY ON CHARGE" in f for f in msg.founder)
    assert store.get_plumber_state(sipho["id"]) is None


def test_model_breaks_a_tie_on_an_unclear_a7_reply(env):
    store, msg, clock, svc, sipho = with_interpreter(env, FakeInterpreter(agreement="disputes"))
    job = to_a7(env)
    assert svc.on_plumber_message(sipho["id"], "I was never even in that suburb that week")
    assert store.get_job(job["id"])["status"] == "disputed"


def test_a_failing_model_falls_back_to_asking(env):
    store, msg, clock, svc, sipho = with_interpreter(env, FakeInterpreter(agreement=None))
    to_a7(env)
    svc.on_plumber_message(sipho["id"], "hmm")
    assert msg.plumber_texts()[-1] == T.a7_clarify()


# ───────────────────────── The model client ─────────────────────────

class _Block:
    type = "text"
    def __init__(self, text): self.text = text


class _Client:
    def __init__(self, reply=None, error=None):
        self.calls, self._reply, self._error = [], reply, error
        self.messages = self

    def create(self, **kwargs):
        self.calls.append(kwargs)
        if self._error:
            raise self._error
        return type("R", (), {"content": [_Block(self._reply)]})()


def test_interpreter_reads_value_and_agreement():
    ci = ClaudeReplyInterpreter(_Client('{"amount_rand": 2400}'))
    assert ci.job_value("two thousand four hundred") == 240000
    ci = ClaudeReplyInterpreter(_Client('{"label": "agrees"}'))
    assert ci.agreement("yebo", "q") == "agrees"


@pytest.mark.parametrize("reply", [
    '{"amount_rand": null}', '{"amount_rand": 3}', '{"amount_rand": 9999999}',
    '{"amount_rand": "2400"}', '{"amount_rand": true}', "not json", "",
])
def test_interpreter_rejects_implausible_values(reply):
    assert ClaudeReplyInterpreter(_Client(reply)).job_value("x") is None


def test_interpreter_failures_mean_no_answer():
    assert ClaudeReplyInterpreter(_Client(error=RuntimeError("down"))).job_value("x") is None
    assert ClaudeReplyInterpreter(_Client('{"label": "maybe"}')).agreement("x", "q") is None
    assert ClaudeReplyInterpreter(_Client(error=RuntimeError("down"))).agreement("x", "q") is None


# ───────────────────────── Trial leads ─────────────────────────

def trial_plumber(store, left, name="Thabo"):
    return store.add_plumber(name, trial_stage=left)


def left(store, plumber):
    return store.get_plumber(plumber["id"]).get("trial_stage")


def test_a_job_taken_on_a_trial_lead_is_free_and_uses_one(env):
    store, msg, clock, svc, _ = env
    thabo = trial_plumber(store, 2)
    job = accept(svc, thabo)
    assert job["fee_cents"] == 0 and "trial_lead" in job["flags"]
    assert store.get_job(job["id"])["fee_cents"] == 0
    assert left(store, thabo) == 1
    assert store.has_event(thabo["id"], "trial_lead_used")


def test_when_the_trial_leads_run_out_the_normal_fee_applies(env):
    store, msg, clock, svc, _ = env
    thabo = trial_plumber(store, 2)
    jobs = [accept(svc, thabo, n=i, phone=f"082{i}000000") for i in range(3)]
    assert [j["fee_cents"] for j in jobs] == [0, 0, 22000]
    assert "trial_lead" not in jobs[2]["flags"]
    assert left(store, thabo) == 0                       # never goes below zero


def test_taking_the_same_lead_twice_uses_only_one_trial_lead(env):
    store, msg, clock, svc, _ = env
    thabo = trial_plumber(store, 3)
    first = accept(svc, thabo)
    again = svc.register_acceptance(
        lead_id=first["lead_id"], plumber_id=thabo["id"], customer_phone="0821111111",
        customer_name="Sarah", suburb="Fourways", keyword="geyser")
    assert again["id"] == first["id"] and left(store, thabo) == 2


def test_a_plumber_who_never_had_trial_leads_pays_the_normal_fee(env):
    store, msg, clock, svc, sipho = env                  # Sipho has no trial_stage at all
    job = accept(svc, sipho)
    assert job["fee_cents"] == 22000 and "trial_lead" not in job["flags"]
    assert left(store, sipho) is None                    # NULL stays NULL


def test_a_trial_job_says_so_when_the_value_is_logged(env):
    store, msg, clock, svc, _ = env
    thabo = trial_plumber(store, 1)
    accept(svc, thabo)
    svc.on_plumber_message(thabo["id"], "done R1800")
    last = msg.plumber_texts()[-1]
    assert "R1,800" in last and "no fee" in last and "invoice" not in last


def test_trial_jobs_are_never_invoiced_and_a_paid_job_still_is(env):
    store, msg, clock, svc, _ = env
    thabo = trial_plumber(store, 1)
    free = accept(svc, thabo, n=1, phone="0821000001")
    svc.on_plumber_message(thabo["id"], "done R1800")             # finish the free one first
    paid = accept(svc, thabo, n=2, phone="0821000002")
    svc.on_plumber_message(thabo["id"], "done R2400")
    assert (free["fee_cents"], paid["fee_cents"]) == (0, 22000)
    assert store.get_job(free["id"])["status"] == store.get_job(paid["id"])["status"] == "charged"
    clock.set(sast(2026, 10, 9, 17, 5))                  # Friday 17:05
    svc.sweep_invoices()
    inv = store.list_invoices(plumber_id=thabo["id"])[0]
    assert inv["total_cents"] == 22000 and inv["job_count"] == 1     # only the paid job
    assert store.get_job(free["id"]).get("invoice_id") is None


def test_a_week_of_only_trial_jobs_sends_nothing(env):
    store, msg, clock, svc, _ = env
    thabo = trial_plumber(store, 1)
    job = accept(svc, thabo)
    svc.on_plumber_message(thabo["id"], "done R1800")
    assert store.get_job(job["id"])["status"] == "charged"
    before = len(msg.plumber_texts())
    clock.set(sast(2026, 10, 9, 17, 5))
    svc.sweep_invoices()
    assert store.list_invoices(plumber_id=thabo["id"]) == []
    assert len(msg.plumber_texts()) == before


def test_if_the_trial_count_cannot_be_updated_the_plumber_can_still_take_the_job(env, monkeypatch):
    store, msg, clock, svc, _ = env
    thabo = trial_plumber(store, 2)

    def broken(plumber_id):
        raise RuntimeError("column trial_stage does not exist")
    monkeypatch.setattr(store, "use_trial_lead", broken)
    job = accept(svc, thabo)
    assert job["fee_cents"] == 22000 and "trial_lead" not in job["flags"]   # full fee, job exists


def test_grant_gives_trial_leads_once_and_never_overwrites(env):
    store, msg, clock, svc, sipho = env
    assert store.grant_trial_leads(sipho["id"], 5) == 5                  # NULL becomes 5
    store.use_trial_lead(sipho["id"])
    assert store.grant_trial_leads(sipho["id"], 5) == 4                  # not reset to 5
    used_up = trial_plumber(store, 0, name="Used")
    assert store.grant_trial_leads(used_up["id"], 5) == 0                # 0 means used them all
    assert store.use_trial_lead(used_up["id"]) is None


def test_the_voice_of_the_new_messages(env):
    # same rules as every other plumber message: no emoji, no exclamation marks
    for text in (T.lead_arrival("Fourways", "geyser", 1, 0), T.trial_lead_taken(1), T.trial_lead_taken(4),
                 T.a5_value_logged(180000, 0), T.a7_customer_confirmed("Thabo", "Sarah", "geyser", "Monday", 0)):
        assert "!" not in text and "R0" not in text


# ───────────────────────── lead credits ─────────────────────────

def credit_plumber(store, n, name="Thabo"):
    return store.add_plumber(name, credits=n)


def test_a_job_taken_with_a_credit_uses_one_and_is_never_invoiced(env):
    store, msg, clock, svc, _ = env
    thabo = credit_plumber(store, 3)
    job = accept(svc, thabo)
    assert job["fee_cents"] == 0 and "credit_lead" in job["flags"]
    assert store.credit_balance(thabo["id"]) == 2
    svc.on_plumber_message(thabo["id"], "done R1800")
    assert "no fee" in msg.plumber_texts()[-1]
    clock.set(sast(2026, 10, 9, 17, 5))
    svc.sweep_invoices()
    assert store.list_invoices(plumber_id=thabo["id"]) == []


def test_trial_lead_comes_before_a_credit(env):
    store, msg, clock, svc, _ = env
    thabo = store.add_plumber("Thabo", trial_stage=1, credits=2)
    first = accept(svc, thabo, n=1, phone="0821000001")
    second = accept(svc, thabo, n=2, phone="0821000002")
    assert "trial_lead" in first["flags"] and "credit_lead" in second["flags"]
    assert store.credit_balance(thabo["id"]) == 1


def test_no_credit_left_keeps_the_job_at_the_full_fee_and_tells_the_founder(env):
    store, msg, clock, svc, _ = env
    thabo = credit_plumber(store, 0)
    job = accept(svc, thabo)
    assert job["fee_cents"] == 22000 and "credit_lead" not in job["flags"]
    assert any("No credit left" in f for f in msg.founder)


def test_a_no_go_gives_the_credit_back_once(env):
    store, msg, clock, svc, _ = env
    thabo = credit_plumber(store, 2)
    job = accept(svc, thabo)
    assert store.credit_balance(thabo["id"]) == 1
    svc.on_plumber_message(thabo["id"], "no go")
    assert store.get_job(job["id"])["status"] == "no_charge"
    assert store.credit_balance(thabo["id"]) == 2
    assert any("credit is back" in t for t in msg.plumber_texts())
    svc.sweep_jobs()
    assert store.credit_balance(thabo["id"]) == 2


def test_resolving_a_dispute_as_no_charge_returns_the_credit(env):
    store, msg, clock, svc, _ = env
    thabo = credit_plumber(store, 1)
    job = accept(svc, thabo)
    store.update_job(job["id"], status="disputed")
    svc.admin_resolve(job["id"], "no_charge", "never happened")
    assert store.credit_balance(thabo["id"]) == 1
    job2 = accept(svc, credit_plumber(store, 1, "Kelo"), n=2, phone="0821000009")
    store.update_job(job2["id"], status="disputed")
    svc.admin_resolve(job2["id"], "charge")
    assert store.credit_balance(job2["plumber_id"]) == 0


def test_one_undeliverable_message_does_not_stop_the_sweep_for_other_jobs(env):
    store, msg, clock, svc, sipho = env
    thabo = store.add_plumber("Thabo")
    accept(svc, sipho, n=1, phone="0821000001")
    accept(svc, thabo, n=2, phone="0821000002")
    original = msg.send_plumber

    def flaky(plumber, text):
        if plumber["first_name"] == "Sipho":
            raise RuntimeError("131047 re-engagement message")
        original(plumber, text)
    msg.send_plumber = flaky
    clock.set(MON + timedelta(days=1, minutes=30))
    svc.sweep_jobs()
    assert any(name == "Thabo" and "still open" in t for name, t in msg.plumber)
