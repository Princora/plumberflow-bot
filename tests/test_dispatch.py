import re
from datetime import timedelta

import pytest

from dispatch import DispatchService
from dispatch.phone import normalize_phone
from dispatch.router import handle_whatsapp_webhook
from dispatch.telegram import handle_telegram_update
from dispatch.whatsapp import extract_messages
from dispatch import templates as DT
from verification import VerificationService
from tests.test_verification import Clock, FakeMessenger, MON


class FakeTelegram:
    def __init__(self):
        self.posts, self.edits, self._id = [], {}, 100

    def post(self, text, buttons=None):
        self._id += 1
        self.posts.append((self._id, text, buttons))
        self.edits[self._id] = (text, buttons)
        return self._id

    def edit(self, mid, text, buttons=None):
        self.edits[mid] = (text, buttons)

    def view(self, mid):
        return self.edits[mid]

    def button_data(self, mid):
        return [b["callback_data"] for row in (self.edits[mid][1] or []) for b in row]

    def find(self, mid, label):
        for row in self.edits[mid][1] or []:
            for b in row:
                if b["text"] == label:
                    return b["callback_data"]
        raise AssertionError(f"no button {label!r} in {self.button_data(mid)}")


@pytest.fixture
def d(store):
    msg, tg, clock = FakeMessenger(), FakeTelegram(), Clock(MON)
    ver = VerificationService(store, msg, clock=clock)
    svc = DispatchService(store, ver, msg, tg)
    sipho = store.add_plumber("Sipho", whatsapp="+27821110001", areas=["Fourways", "Randburg"], credits=10)
    thabo = store.add_plumber("Thabo", whatsapp="0821110002", areas=["Fourways"], credits=10)
    kelo = store.add_plumber("Kelo", whatsapp="+27821110003", areas=["Mondeor"], credits=10)
    return store, msg, tg, clock, ver, svc, sipho, thabo, kelo


def lead(svc, suburb="Fourways", keyword="geyser"):
    return svc.new_lead(customer_phone="0829998888", customer_name="Sarah", suburb=suburb,
                        keyword=keyword, address="12 Cedar Rd", summary="Geyser leaking")


def p8(p):
    return p["id"].replace("-", "")[:8]


def texts(msg, name):
    return [t for n, t in msg.plumber if n == name]


# ───────────── Telegram post ─────────────

def test_new_lead_posts_to_telegram_with_matching_buttons(d):
    store, msg, tg, clock, ver, svc, sipho, thabo, kelo = d
    l = lead(svc)
    assert l["ref"].startswith("L-") and l["status"] == "open"
    mid = l["telegram_message_id"]
    text, _ = tg.view(mid)
    assert "Fourways" in text and "Sarah" in text and "0829998888" in text
    labels = [b["text"] for row in tg.view(mid)[1] for b in row]
    assert "Offer to Sipho" in labels and "Offer to Thabo" in labels
    assert "Offer to Kelo" not in labels            # Mondeor, not Fourways
    assert "Offer to 2 matching" in labels and "Skip" in labels
    assert all(len(c.encode()) <= 64 for c in tg.button_data(mid))


def test_no_matching_plumber_still_offers_pick_and_skip(d):
    store, msg, tg, *_ = d[:3] + d[3:]
    svc = d[5]
    l = lead(svc, suburb="Sandton")
    labels = [b["text"] for row in tg.view(l["telegram_message_id"])[1] for b in row]
    assert labels == ["Pick a plumber", "Skip"]


# ───────────── Offer ─────────────

def test_offer_sends_suburb_and_problem_only(d):
    store, msg, tg, clock, ver, svc, sipho, thabo, kelo = d
    l = lead(svc)
    svc.on_telegram_callback(tg.find(l["telegram_message_id"], "Offer to Sipho"))
    [text] = texts(msg, "Sipho")
    assert "Fourways" in text and "Geyser leaking" in text and "Uses 1 credit" in text
    assert "Sarah" not in text and "0829998888" not in text and "Cedar" not in text
    assert l["ref"] in text and "take" in text
    assert store.get_lead(l["ref"])["status"] == "offered"
    assert "Offered: Sipho" in tg.view(l["telegram_message_id"])[0]


def test_offer_to_matching_and_no_duplicate_offer(d):
    store, msg, tg, clock, ver, svc, sipho, thabo, kelo = d
    l = lead(svc)
    mid = l["telegram_message_id"]
    svc.on_telegram_callback(f"m:{l['ref']}")
    assert len(texts(msg, "Sipho")) == 1 and len(texts(msg, "Thabo")) == 1
    assert "2 plumbers viewing" in texts(msg, "Sipho")[0]
    svc.on_telegram_callback(f"o:{l['ref']}:{p8(sipho)}")      # tapped again
    assert len(texts(msg, "Sipho")) == 1


def test_paused_plumber_needs_explicit_override(d):
    store, msg, tg, clock, ver, svc, sipho, *_ = d
    store.add_hold(sipho["id"], "unresolved_jobs")
    l = lead(svc)
    mid = l["telegram_message_id"]
    labels = [b["text"] for row in tg.view(mid)[1] for b in row]
    assert "Offer to Sipho" not in labels            # not suggested while paused
    assert svc.on_telegram_callback(f"o:{l['ref']}:{p8(sipho)}") == "Paused"
    assert texts(msg, "Sipho") == []
    assert "paused" in tg.view(mid)[0]
    svc.on_telegram_callback(tg.find(mid, "Offer anyway"))
    assert len(texts(msg, "Sipho")) == 1


def test_skip_closes_lead(d):
    store, msg, tg, clock, ver, svc, *_ = d
    l = lead(svc)
    svc.on_telegram_callback(f"s:{l['ref']}")
    assert store.get_lead(l["ref"])["status"] == "skipped"
    assert svc.on_telegram_callback(f"m:{l['ref']}").startswith("Already")
    assert tg.view(l["telegram_message_id"])[1] is None


# ───────────── Take ─────────────

def test_take_creates_job_sends_details_and_closes_losers(d):
    store, msg, tg, clock, ver, svc, sipho, thabo, kelo = d
    l = lead(svc)
    svc.on_telegram_callback(f"m:{l['ref']}")
    assert svc.on_whatsapp_text("27821110001", "take") is True
    job = store.get_job_by_lead(l["ref"])
    assert job and job["plumber_id"] == sipho["id"] and job["customer_phone"] == "0829998888"
    assert job["status"] == "accepted" and job["fee_cents"] == 0 and "credit_lead" in job["flags"]
    confirm = texts(msg, "Sipho")[-1]
    assert "Sarah" in confirm and "12 Cedar Rd" in confirm and "done" in confirm
    assert "taken by another" in texts(msg, "Thabo")[-1]
    assert store.get_lead(l["ref"])["status"] == "taken"
    assert "Taken by Sipho" in tg.view(l["telegram_message_id"])[0]
    assert tg.view(l["telegram_message_id"])[1] is None


def test_first_take_wins_second_is_told_it_is_gone(d):
    store, msg, tg, clock, ver, svc, sipho, thabo, kelo = d
    l = lead(svc)
    svc.on_telegram_callback(f"m:{l['ref']}")
    svc.on_whatsapp_text("0821110002", "Take")          # Thabo, national format
    assert svc.on_whatsapp_text("+27821110001", "take") is True
    assert "taken before your reply" in texts(msg, "Sipho")[-1]
    assert store.get_job_by_lead(l["ref"])["plumber_id"] == thabo["id"]
    assert len(store.list_jobs()) == 1


def test_claim_is_atomic_at_the_store(d):
    store, msg, tg, clock, ver, svc, sipho, thabo, kelo = d
    l = lead(svc)
    assert store.claim_lead(l["ref"], sipho["id"], clock()) is True
    assert store.claim_lead(l["ref"], thabo["id"], clock()) is False


@pytest.mark.parametrize("text", ["take", "Take it", "yes", "I'll take it", "take.", "accept"])
def test_take_phrasings(d, text):
    store, msg, tg, clock, ver, svc, sipho, *_ = d
    l = lead(svc)
    svc.on_telegram_callback(f"o:{l['ref']}:{p8(sipho)}")
    assert svc.on_whatsapp_text("27821110001", text)
    assert store.get_job_by_lead(l["ref"])


def test_yes_with_nothing_open_falls_through(d):
    store, msg, tg, clock, ver, svc, sipho, *_ = d
    assert svc.on_whatsapp_text("27821110001", "yes") is False      # verification: nothing to do
    assert texts(msg, "Sipho") == []


def test_two_open_offers_require_a_code(d):
    store, msg, tg, clock, ver, svc, sipho, *_ = d
    a, b = lead(svc), lead(svc, keyword="blocked drain")
    for l in (a, b):
        svc.on_telegram_callback(f"o:{l['ref']}:{p8(sipho)}")
    svc.on_whatsapp_text("27821110001", "take")
    assert a["ref"] in texts(msg, "Sipho")[-1] and b["ref"] in texts(msg, "Sipho")[-1]
    assert store.list_jobs() == []
    n = int(b["ref"].split("-")[1])
    svc.on_whatsapp_text("27821110001", f"take {n}")
    assert store.get_job_by_lead(b["ref"]) and not store.get_job_by_lead(a["ref"])


def test_unknown_number_is_not_a_plumber(d):
    store, msg, tg, clock, ver, svc, *_ = d
    lead(svc)
    assert svc.on_whatsapp_text("27830000000", "take") is False


def test_done_still_reaches_verification_after_take(d):
    store, msg, tg, clock, ver, svc, sipho, *_ = d
    l = lead(svc)
    svc.on_telegram_callback(f"o:{l['ref']}:{p8(sipho)}")
    svc.on_whatsapp_text("27821110001", "take")
    assert svc.on_whatsapp_text("27821110001", "done R2400") is True
    assert store.get_job_by_lead(l["ref"])["plumber_claim"] == "done"


# ───────────── Sweeps ─────────────

def test_no_taker_alert_after_ttl_once(d):
    store, msg, tg, clock, ver, svc, sipho, *_ = d
    l = lead(svc)
    svc.on_telegram_callback(f"o:{l['ref']}:{p8(sipho)}")
    n = len(tg.posts)
    clock.set(clock() + timedelta(minutes=29)); svc.run_sweeps()
    assert len(tg.posts) == n
    clock.set(clock() + timedelta(minutes=2)); svc.run_sweeps(); svc.run_sweeps()
    assert len(tg.posts) == n + 1 and "No reply" in tg.posts[-1][1]
    assert any("Offer to Thabo" in b["text"] for row in tg.posts[-1][2] for b in row)


def test_late_take_after_alert_still_works(d):
    store, msg, tg, clock, ver, svc, sipho, *_ = d
    l = lead(svc)
    svc.on_telegram_callback(f"o:{l['ref']}:{p8(sipho)}")
    clock.set(clock() + timedelta(minutes=45)); svc.run_sweeps()
    svc.on_whatsapp_text("27821110001", "take")
    assert store.get_job_by_lead(l["ref"])
    latest = store.get_lead(l["ref"])["telegram_message_id"]       # the re-posted message
    assert "Taken by Sipho" in tg.view(latest)[0]


# ───────────── Adapters ─────────────

def test_only_the_founder_can_press_buttons(d):
    store, msg, tg, clock, ver, svc, *_ = d
    l = lead(svc)

    class Client:
        answered = []
        def answer_callback(self, i, t=""): self.answered.append((i, t))

    c = Client()
    upd = lambda uid: {"callback_query": {"id": "1", "from": {"id": uid}, "data": f"s:{l['ref']}"}}
    assert handle_telegram_update(upd(999), svc, c, founder_user_id=111) is False
    assert store.get_lead(l["ref"])["status"] == "open"
    assert handle_telegram_update(upd(111), svc, c, founder_user_id=111) is True
    assert store.get_lead(l["ref"])["status"] == "skipped"


def wa_payload(phone, **m):
    return {"entry": [{"changes": [{"value": {"messages": [{"from": phone, **m}]}}]}]}


def test_webhook_routing(d):
    store, msg, tg, clock, ver, svc, sipho, *_ = d
    l = lead(svc)
    svc.on_telegram_callback(f"o:{l['ref']}:{p8(sipho)}")
    seen, unhandled = [], []
    route = lambda payload: handle_whatsapp_webhook(
        payload, svc, ver, on_customer_text=lambda p, t: seen.append((p, t)),
        on_plumber_unhandled=lambda p, t: unhandled.append((p, t)))
    route(wa_payload("27821110001", type="text", text={"body": "take"}))
    assert store.get_job_by_lead(l["ref"]) and seen == []
    route(wa_payload("27829998888", type="text", text={"body": "hi, still need help"}))
    assert seen == [("27829998888", "hi, still need help")]
    route(wa_payload("27821110001", type="text", text={"body": "what is going on"}))
    assert unhandled == [("27821110001", "what is going on")] and len(seen) == 1
    job = store.get_job_by_lead(l["ref"])
    route(wa_payload("27829998888", type="interactive",
                     interactive={"button_reply": {"id": f"{job['id']}|yes", "title": "Yes"}}))
    assert store.get_job(job["id"])["customer_response"] == "yes"


def test_extract_messages_ignores_statuses():
    assert list(extract_messages({"entry": [{"changes": [{"value": {"statuses": [{}]}}]}]})) == []


@pytest.mark.parametrize("raw", ["0796051126", "+27 79 605 1126", "27796051126", "0027796051126"])
def test_phone_normalisation(raw):
    assert normalize_phone(raw) == "27796051126"


def test_dispatch_wording_follows_voice_rules():
    sample = [
        DT.offer_to_plumber("L-0001", "Fourways", "Geyser", 2, 22000),
        DT.which_offer([("L-0001", "Fourways geyser")]),
        DT.gone("Fourways geyser"), DT.lost_to_other("Fourways geyser"),
    ]
    banned = re.compile(r"trusted|vetted|valued|premium|welcome to", re.I)
    for s in sample:
        assert "!" not in s and not banned.search(s)
        assert not re.search(r"[\U0001F300-\U0001FAFF☀-➿]", s)
