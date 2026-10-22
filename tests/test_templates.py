"""Per-message WhatsApp templates: the fallback when Meta refuses free text (window closed),
the parameters sent, the button template, and a check that the code and WHATSAPP_TEMPLATES.md agree."""

import pathlib
import re

import pytest

import main
from dispatch import templates as DT
from dispatch.whatsapp import CloudMessenger, WhatsAppCloud, WhatsAppError, extract_messages
from verification import templates as VT
from tests.test_app import PLUMBER_PHONE, env  # noqa: F401  (env is the app fixture)

DOC = (pathlib.Path(__file__).resolve().parent.parent / "WHATSAPP_TEMPLATES.md").read_text()


class Meta:
    """Stands in for Meta. Free text and buttons are refused while window_closed; a template is
    refused if its name is in `unapproved`."""

    def __init__(self, window_closed=True, unapproved=()):
        self.window_closed, self.unapproved, self.sent = window_closed, set(unapproved), []

    def post(self, url, token, payload):
        kind = payload["type"]
        if kind in ("text", "interactive") and self.window_closed:
            raise WhatsAppError(400, "131047 re-engagement message")
        if kind == "template" and payload["template"]["name"] in self.unapproved:
            raise WhatsAppError(404, "132001 template does not exist")
        self.sent.append(payload)
        return {}

    def messenger(self, **kw):
        return CloudMessenger(WhatsAppCloud("T", "PNID", post=self.post), telegram=None, **kw)


def offer():
    return DT.offer_to_plumber("L-0412", "Randburg", "Burst pipe · Emergency — right now", 1, 22000, 4)


def test_a_message_in_the_window_is_sent_as_plain_text():
    meta = Meta(window_closed=False)
    meta.messenger().send_plumber({"whatsapp": "27821110000"}, offer())
    [payload] = meta.sent
    assert payload["type"] == "text" and "New job — Randburg" in payload["text"]["body"]


def test_outside_the_window_the_offer_goes_as_its_own_template():
    meta = Meta()
    meta.messenger().send_plumber({"whatsapp": "27821110000"}, offer())
    [payload] = meta.sent
    assert payload["type"] == "template" and payload["template"]["name"] == "pf_job_offer"
    assert payload["template"]["language"] == {"code": "en"}
    assert [p["text"] for p in payload["template"]["components"][0]["parameters"]] == [
        "Randburg", "Burst pipe · Emergency — right now", "1 plumber", "Uses 1 credit · you have 4", "L-0412"]


def test_the_template_language_is_configurable():
    meta = Meta()
    meta.messenger(template_lang="en_ZA").send_plumber({"whatsapp": "27821110000"}, offer())
    assert meta.sent[0]["template"]["language"] == {"code": "en_ZA"}


def test_trial_offer_says_trial_lead():
    text = DT.offer_to_plumber("L-0001", "Fourways", "Leak", 2, 0, None)
    assert text.params[2:4] == ["2 plumbers", "Trial lead · no fee"]


def test_a_template_meta_has_not_approved_falls_back_to_the_catch_all():
    meta = Meta(unapproved={"pf_job_offer"})
    meta.messenger(fallback_template="plumber_message").send_plumber({"whatsapp": "27821110000"}, offer())
    [payload] = meta.sent
    assert payload["template"]["name"] == "plumber_message"
    [param] = payload["template"]["components"][0]["parameters"]
    assert "\n" not in param["text"] and "Randburg" in param["text"]


def test_with_nothing_approved_and_no_catch_all_the_error_is_raised():
    meta = Meta(unapproved={"pf_job_offer"})
    with pytest.raises(WhatsAppError):
        meta.messenger().send_plumber({"whatsapp": "27821110000"}, offer())


def test_a_plain_message_outside_the_window_still_uses_only_the_catch_all():
    meta = Meta()
    meta.messenger(fallback_template="plumber_message").send_plumber({"whatsapp": "27821110000"}, "Hello there")
    assert meta.sent[0]["template"]["name"] == "plumber_message"
    with pytest.raises(WhatsAppError):
        Meta().messenger().send_plumber({"whatsapp": "27821110000"}, "Hello there")


def test_template_values_never_contain_line_breaks_or_are_empty():
    meta = Meta()
    msg = VT.Templated("x", "pf_job_close_prompt", ["Sipho\nDlamini", "", "burst   pipe"])
    meta.messenger().send_plumber({"whatsapp": "27821110000"}, msg)
    values = [p["text"] for p in meta.sent[0]["template"]["components"][0]["parameters"]]
    assert values == ["Sipho Dlamini", "-", "burst pipe"]


def test_the_customer_buttons_become_a_template_with_two_quick_replies():
    meta = Meta()
    text = VT.b1_confirm_logged("Thandi", "Sipho", "burst pipe", 240000)
    meta.messenger().send_customer_buttons("27829990000", text, VT.B1_BUTTONS, "job-1")
    [payload] = meta.sent
    tpl = payload["template"]
    assert tpl["name"] == "pf_confirm_logged"
    assert [p["text"] for p in tpl["components"][0]["parameters"]] == ["Thandi", "Sipho", "burst pipe", "R2,400"]
    buttons = tpl["components"][1:]
    assert [(b["sub_type"], b["index"], b["parameters"][0]["payload"]) for b in buttons] == [
        ("quick_reply", "0", "job-1|yes"), ("quick_reply", "1", "job-1|off")]


def test_the_customer_buttons_are_still_interactive_inside_the_window():
    meta = Meta(window_closed=False)
    meta.messenger().send_customer_buttons("27829990000", VT.b1_confirm_logged("T", "S", "k", 100), VT.B1_BUTTONS, "j")
    assert meta.sent[0]["type"] == "interactive"


def test_a_tap_on_a_template_button_is_read_like_an_interactive_tap():
    webhook = {"entry": [{"changes": [{"value": {"messages": [
        {"from": "27829990000", "type": "button", "button": {"payload": "job-1|yes", "text": "Yes"}}]}}]}]}
    [tap] = list(extract_messages(webhook))
    assert tap == {"phone": "27829990000", "kind": "button", "id": "job-1|yes", "title": "Yes"}


# ───────────── the messages sent from main.py (approval, credits) ─────────────

def close_the_window(env):
    sent = []

    def post(url, token, payload):
        if payload["type"] == "template":
            sent.append(payload["template"])
            return {}
        raise WhatsAppError(400, "131047 re-engagement message")
    env.services.wa._post = post
    return sent


def test_approval_goes_as_a_template_when_the_window_has_closed(env):
    from tests.test_app import p8
    p = env.store.add_plumber("Lerato", whatsapp=PLUMBER_PHONE, areas=["randburg"])
    env.store.add_hold(p["id"], main.HOLD_PENDING)
    sent = close_the_window(env)
    env.founder_says(f"/approve {p8(p)}")
    [tpl] = sent
    assert tpl["name"] == "pf_approved"
    assert [x["text"] for x in tpl["components"][0]["parameters"]] == [
        "Lerato", f"You have {main.TRIAL_LEADS} free trial leads, no fee on those."]


def test_credits_added_goes_as_a_template_when_the_window_has_closed(env):
    p = env.add_plumber(credits=1)
    order = env.store.create_credit_order(p["id"], 5, 104_500)
    sent = close_the_window(env)
    env.founder_says(f"/credit {order['ref']}")
    [tpl] = sent
    assert tpl["name"] == "pf_credits_added"
    assert [x["text"] for x in tpl["components"][0]["parameters"]] == ["Sipho", "5", "6"]


# ───────────── the doc and the code agree ─────────────

def doc_placeholder_counts():
    out = {}
    for m in re.finditer(r"\*\*(pf_\w+)\*\*[^\n]*\n(?:[^\n]*\n)*?```\n(.*?)```", DOC, re.S):
        nums = [int(n) for n in re.findall(r"\{\{(\d+)\}\}", m.group(2))]
        out[m.group(1)] = max(nums) if nums else 0
    return out


EXAMPLES = {
    "pf_job_offer": offer(),
    "pf_job_taken_elsewhere": DT.lost_to_other("Randburg burst pipe"),
    "pf_job_close_prompt": VT.a3_job_close_prompt("Sipho", "Randburg", "burst pipe"),
    "pf_customer_confirmed": VT.a7_customer_confirmed("Sipho", "Thandi", "burst pipe", "Tuesday", 0),
    "pf_open_jobs_pattern": VT.a9_pattern("Sipho", [(1, "Randburg leak", "Tue"), (2, "Fourways pipe", "Wed")]),
    "pf_credit_returned": VT.credit_returned("Randburg leak", 4, "Sipho"),
    "pf_confirm_logged": VT.b1_confirm_logged("Thandi", "Sipho", "leak", 240000),
    "pf_did_it_get_sorted": VT.b2_did_it_get_sorted("Thandi", "Sipho", "leak"),
    "pf_approved": VT.Templated("x", "pf_approved", ["Sipho", "line"]),
    "pf_credits_added": VT.Templated("x", "pf_credits_added", ["Sipho", 5, 6]),
}


@pytest.mark.parametrize("name", sorted(EXAMPLES))
def test_each_templates_parameter_count_matches_the_doc(name):
    counts = doc_placeholder_counts()
    assert name in counts, f"{name} is not described in WHATSAPP_TEMPLATES.md"
    assert EXAMPLES[name].template == name
    assert len(EXAMPLES[name].params) == counts[name]
