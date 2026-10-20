"""Lead credits: pack prices, the buy flow, founder confirmation, using and refunding a credit."""

import pytest

import credits as C
import main
from tests.test_app import FOUNDER, PLUMBER_PHONE, env, p8  # noqa: F401  (env is the app fixture)


# ───────────────────────── prices ─────────────────────────

@pytest.mark.parametrize("pack,cents", [(2, 44_000), (5, 104_500), (10, 198_000)])
def test_pack_prices(pack, cents):
    assert C.pack_price_cents(pack) == cents


def test_bigger_packs_are_cheaper_per_credit():
    per = [C.pack_price_cents(n) / n for n in C.PACK_SIZES]
    assert per == sorted(per, reverse=True) and per[0] == C.CREDIT_PRICE_CENTS


@pytest.mark.parametrize("text,pack", [("2", 2), ("5", 5), ("10", 10), (" 5 credits ", 5), ("buy 10", 10),
                                       ("3", None), ("20", None), ("hello", None), ("", None)])
def test_parse_pack(text, pack):
    assert C.parse_pack(text) == pack


def test_unknown_pack_has_no_price():
    with pytest.raises(ValueError):
        C.pack_price_cents(3)


# ───────────────────────── buying ─────────────────────────

def founder_buttons(env):
    for method, payload in reversed(env.tg_calls):
        if method in ("sendMessage", "editMessageText") and payload.get("reply_markup"):
            return [b["callback_data"] for row in payload["reply_markup"]["inline_keyboard"] for b in row]
    return []


def buy(env, pack="5", credits=0):
    p = env.add_plumber(credits=credits)
    env.post_wa(PLUMBER_PHONE, text="BUY")
    env.post_wa(PLUMBER_PHONE, text=pack)
    return p


def test_buy_menu_shows_the_packs_and_prices(env):
    env.add_plumber(credits=0)
    env.post_wa(PLUMBER_PHONE, text="buy")
    menu = env.wa_text_to(PLUMBER_PHONE)[-1]
    assert "R440" in menu and "R1,045 (5% off)" in menu and "R1,980 (10% off)" in menu
    assert "You have 0 credits" in menu


def test_choosing_a_pack_creates_an_order_and_tells_the_founder(env, monkeypatch):
    monkeypatch.setenv("CREDIT_PAYMENT_DETAILS", "FNB|Acc 123456")
    p = buy(env, "5")
    [order] = env.store.list_credit_orders(plumber_id=p["id"])
    assert (order["credits"], order["price_cents"], order["status"]) == (5, 104_500, "pending")
    reply = env.wa_text_to(PLUMBER_PHONE)[-1]
    assert order["ref"] in reply and "R1,045" in reply and "FNB\nAcc 123456" in reply
    assert any(order["ref"] in t and "/credit" in t and "5 credits" in t for t in env.tg_texts())
    assert env.store.credit_balance(p["id"]) == 0          # nothing until the founder confirms


def test_a_wrong_pack_is_asked_again_and_cancel_leaves_no_order(env):
    p = env.add_plumber(credits=0)
    env.post_wa(PLUMBER_PHONE, text="BUY")
    env.post_wa(PLUMBER_PHONE, text="3")
    assert "Please reply with" in env.wa_text_to(PLUMBER_PHONE)[-1]
    env.post_wa(PLUMBER_PHONE, text="CANCEL")
    assert env.store.list_credit_orders(plumber_id=p["id"]) == []


def test_asking_for_the_same_pack_twice_reuses_the_open_order(env):
    p = buy(env, "2")
    env.post_wa(PLUMBER_PHONE, text="BUY")
    env.post_wa(PLUMBER_PHONE, text="2")
    assert len(env.store.list_credit_orders(plumber_id=p["id"])) == 1


def test_without_bank_details_configured_the_plumber_is_told_to_wait(env, monkeypatch):
    monkeypatch.delenv("CREDIT_PAYMENT_DETAILS", raising=False)
    buy(env, "2")
    assert "bank details shortly" in env.wa_text_to(PLUMBER_PHONE)[-1]


# ───────────────────────── founder confirms ─────────────────────────

def test_founder_confirms_the_eft_and_credits_arrive_once(env):
    p = buy(env, "10", credits=1)
    ref = env.store.list_credit_orders(plumber_id=p["id"])[0]["ref"]
    env.founder_says("/orders")
    assert ref in env.tg_texts()[-1]
    env.founder_says(f"/credit {ref}")
    assert env.store.credit_balance(p["id"]) == 11
    assert "Payment received" in env.wa_text_to(PLUMBER_PHONE)[-1] and "11 credits" in env.wa_text_to(PLUMBER_PHONE)[-1]
    env.founder_says(f"/credit {ref}")                              # a second tap adds nothing
    assert env.store.credit_balance(p["id"]) == 11
    assert "already paid" in env.tg_texts()[-1]
    env.founder_says("/orders")
    assert "No credit orders" in env.tg_texts()[-1]


def test_founder_can_cancel_an_order_and_it_cannot_then_be_paid(env):
    p = buy(env, "2")
    ref = env.store.list_credit_orders(plumber_id=p["id"])[0]["ref"]
    env.founder_says(f"/credit {ref} cancel")
    env.founder_says(f"/credit {ref}")
    assert env.store.credit_balance(p["id"]) == 0


def test_credit_commands_ignore_anyone_but_the_founder(env):
    p = buy(env, "2")
    ref = env.store.list_credit_orders(plumber_id=p["id"])[0]["ref"]
    env.post_tg({"message": {"from": {"id": FOUNDER + 1}, "chat": {"id": 1}, "text": f"/credit {ref}"}})
    assert env.store.credit_balance(p["id"]) == 0


def test_unknown_order_and_grant(env):
    p = env.add_plumber(credits=0)
    env.founder_says("/credit C-9999")
    assert "No order" in env.tg_texts()[-1]
    env.founder_says(f"/grant {p8(p)} 3")
    assert env.store.credit_balance(p["id"]) == 3
    assert "3 credits added" in env.wa_text_to(PLUMBER_PHONE)[-1]
    env.founder_says(f"/grant {p8(p)} 500")
    assert env.store.credit_balance(p["id"]) == 3


# ───────────────────────── using credits ─────────────────────────

def test_status_shows_credits(env):
    env.add_plumber(credits=4)
    env.post_wa(PLUMBER_PHONE, text="status")
    assert "Credits: 4" in env.wa_text_to(PLUMBER_PHONE)[-1]


def test_a_plumber_out_of_credits_is_not_suggested_and_is_told_to_buy_if_offered_anyway(env):
    sipho = env.add_plumber(credits=0)
    env.run_lead_flow()
    assert not any(b == f"o:L-0001:{p8(sipho)}" for b in founder_buttons(env))
    env.founder_tap(f"f:L-0001:{p8(sipho)}")                          # founder insists
    assert "you have none, reply BUY" in env.wa_text_to(PLUMBER_PHONE)[-1]
    env.post_wa(PLUMBER_PHONE, text="take")
    assert "out of credits" in env.wa_text_to(PLUMBER_PHONE)[-1]
    assert env.store.get_job_by_lead("L-0001") is None                 # the lead stays open
    assert env.store.get_lead("L-0001")["status"] == "offered"


def test_taking_the_last_credit_says_so(env):
    sipho = env.add_plumber(credits=1)
    env.run_lead_flow()
    env.founder_tap(f"o:L-0001:{p8(sipho)}")
    env.post_wa(PLUMBER_PHONE, text="take")
    assert env.store.credit_balance(sipho["id"]) == 0
    assert "last credit" in env.wa_text_to(PLUMBER_PHONE)[-1]


def test_a_trial_lead_is_used_before_a_credit(env):
    sipho = env.add_plumber(credits=3)
    env.store.plumbers[sipho["id"]]["trial_stage"] = 2
    env.run_lead_flow()
    env.founder_tap(f"o:L-0001:{p8(sipho)}")
    env.post_wa(PLUMBER_PHONE, text="take")
    job = env.store.get_job_by_lead("L-0001")
    assert "trial_lead" in job["flags"] and "credit_lead" not in job["flags"]
    assert (env.store.plumbers[sipho["id"]]["trial_stage"], env.store.credit_balance(sipho["id"])) == (1, 3)


# ───────────────────────── the store, in memory and on Postgres ─────────────────────────

def test_use_credit_stops_at_zero(store):
    p = store.add_plumber("Sipho", credits=2)
    assert [store.use_credit(p["id"]) for _ in range(3)] == [1, 0, None]
    assert store.credit_balance(p["id"]) == 0


def test_an_order_pays_once_and_adds_its_credits_once(store):
    p = store.add_plumber("Sipho", credits=1)
    order = store.create_credit_order(p["id"], 5, 104_500)
    assert order["ref"].startswith("C-") and order["status"] == "pending"
    first = store.settle_credit_order(order["ref"])
    assert (first["added"], first["balance"]) == (5, 6)
    assert store.settle_credit_order(order["ref"]) is None
    assert store.settle_credit_order(order["ref"].lower()) is None
    assert store.credit_balance(p["id"]) == 6
    assert store.get_credit_order(order["ref"])["status"] == "paid"


def test_a_cancelled_order_cannot_be_paid_and_a_paid_one_cannot_be_cancelled(store):
    p = store.add_plumber("Sipho", credits=0)
    a = store.create_credit_order(p["id"], 2, 44_000)
    b = store.create_credit_order(p["id"], 10, 198_000)
    assert store.cancel_credit_order(a["ref"]) is True
    assert store.settle_credit_order(a["ref"]) is None
    store.settle_credit_order(b["ref"])
    assert store.cancel_credit_order(b["ref"]) is False
    assert store.credit_balance(p["id"]) == 10
    assert [o["ref"] for o in store.list_credit_orders(status="pending")] == []
    assert len(store.list_credit_orders(plumber_id=p["id"])) == 2


def test_add_credits_and_refund_arithmetic(store):
    p = store.add_plumber("Sipho", credits=0)
    assert store.add_credits(p["id"], 3) == 3
    assert store.add_credits(p["id"], -1) is None
    assert store.credit_balance(p["id"]) == 3
