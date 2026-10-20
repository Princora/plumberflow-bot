"""Every outbound message in the verification flow. Deterministic, no model.

Voice rules (enforced by tests/test_verification.py::test_voice_lint):
  no emojis, no exclamation marks, no marketing words, no "Hi there".
Plumber messages are Prince's voice ("— P" is added by the caller only where the
library signs off). Customer messages are the platform's voice: no sign-off.
"""

from __future__ import annotations


def rand(cents: int) -> str:
    if cents % 100:
        return f"R{cents / 100:,.2f}"
    return f"R{cents // 100:,}"


def _plural(n: int, one: str, many: str) -> str:
    return f"{n} {one if n == 1 else many}"


# ───────────────────────── Plumber side ─────────────────────────

def lead_arrival(suburb: str, job_short: str, n_viewing: int, fee_cents: int, credits_left: int | None = None) -> str:
    """A1. Photo is attached by the caller.
    fee_cents 0 is a trial lead. Otherwise, with credits_left given, the lead costs one credit."""
    if fee_cents == 0:
        cost = "Trial lead · no fee"
    elif credits_left is None:
        cost = f"{rand(fee_cents)} due after the job is done"
    elif credits_left > 0:
        cost = f"Uses 1 credit · you have {credits_left}"
    else:
        cost = "Uses 1 credit · you have none, reply BUY to top up"
    return (
        f"New job — {suburb}\n"
        f"{job_short}\n\n"
        f"{_plural(n_viewing, 'plumber', 'plumbers')} viewing\n{cost}"
    )


def take_confirmed(customer_name: str, customer_phone: str, address: str) -> str:
    """A2."""
    return (
        "You've got it.\n\n"
        f"{customer_name} · {customer_phone}\n"
        f"{address}\n\n"
        "Customer expects you to WhatsApp within 10 minutes.\n\n"
        'When you\'re done, send "done" here.'
    )


def a3_job_close_prompt(first_name: str, suburb: str, keyword: str) -> str:
    return (
        f"{first_name} — {suburb} {keyword} still open on my side.\n\n"
        'Done? Send "done" and what you charged.\n\n'
        'If it fell through, send "no go" — nothing owed.'
    )


def a4_ask_value() -> str:
    return "Good. What did you charge?"


def a4_value_retry() -> str:
    return "Amount in rand — for example 2400."


def a4_confirm_value(job_value_cents: int) -> str:
    return f"{rand(job_value_cents)}. Is that right? Reply YES, or send the right amount."


def trial_lead_taken(left: int) -> str:
    return f"This one is a trial lead, so no fee. {_plural(left, 'trial lead', 'trial leads')} left."


def credit_lead_taken(left: int) -> str:
    if left == 0:
        return "That used your last credit. Reply BUY to get more before the next lead."
    return f"That used 1 credit. {_plural(left, 'credit', 'credits')} left."


def out_of_credits() -> str:
    return "You're out of credits, so I can't give you this one yet. Reply BUY to top up, then reply take again."


def credit_returned(job_short: str, balance: int) -> str:
    return (f"The {job_short} job didn't go ahead, so your credit is back. "
            f"You have {_plural(balance, 'credit', 'credits')}.")


def a5_value_logged(job_value_cents: int, fee_cents: int) -> str:
    return (
        f"Logged — {rand(job_value_cents)}.\n\n"
        + ("Nothing owed, no fee on this one.\n\n" if fee_cents == 0
           else f"{rand(fee_cents)} on Friday's invoice.\n\n") +
        "Photo of the finished work if you have one — helps if the customer ever queries it."
    )


def a6_no_go_ack() -> str:
    return (
        "Noted. Nothing owed.\n\n"
        "What happened? One line — helps me stop sending you leads that go the same way."
    )


def a6_reason_ack() -> str:
    return "Noted."


def a7_customer_confirmed(
    first_name: str, customer_name: str, keyword: str, job_date: str, fee_cents: int
) -> str:
    return (
        f"{first_name} — {customer_name} confirmed the {keyword} was sorted on {job_date}.\n\n"
        + ("Nothing owed, no fee on this one.\n\nIf that's wrong, tell me now."
           if fee_cents == 0 else
           f"{rand(fee_cents)} on this week's invoice.\n\nIf that's wrong, tell me now and I'll pull it.")
    )


def a7_clarify() -> str:
    return "Is the charge right? Reply RIGHT to leave it, or WRONG and I'll pull it."


def a7_pulled() -> str:
    return "Pulled. Nothing owed on that one. I'll look into it."


def a9_pattern(first_name: str, jobs: list[tuple[int, str, str]]) -> str:
    """jobs: [(n, short, date)]."""
    lines = "\n".join(f"{n}. {short} · {date}" for n, short, date in jobs)
    return (
        f"{first_name} — {len(jobs)} jobs open on my side with no update:\n\n"
        f"{lines}\n\n"
        "Done, or no go? Reply like this: 1 done R2400, 2 no go\n\n"
        "Leads pause until these are closed off."
    )


def a9_progress(n_logged: int, n_still_open: int) -> str:
    if n_still_open:
        return f"Logged {n_logged}. {n_still_open} still open."
    return f"Logged {n_logged}. All closed off. Leads are back on."


def which_one(options: list[tuple[int, str, str]]) -> str:
    lines = "\n".join(f"{n}. {short} · {date}" for n, short, date in options)
    return f"Which one?\n\n{lines}"


def no_open_jobs() -> str:
    return "Nothing open on my side for you."


def a10_invoice(
    first_name: str, date_range: str, lines: list[str], total_cents: int
) -> str:
    body = "\n".join(lines)
    return (
        f"Week of {date_range} — {first_name}\n\n"
        f"{_plural(len(lines), 'job', 'jobs')} completed\n"
        f"{body}\n\n"
        f"{rand(total_cents)} due\n\n"
        "PayShap to 082 XXX XXXX. Due Monday end-of-day."
    )


def a10_zero_week(first_name: str, date_range: str, n_flowed: int | None, suburb: str | None) -> str:
    msg = f"Week of {date_range} — {first_name}\n\nNo completed jobs. Nothing owed."
    if n_flowed is not None and suburb:
        msg += f"\n\n{n_flowed} jobs flowed in {suburb}."
    return msg


def a11_overdue(first_name: str, total_cents: int) -> str:
    return (
        f"{first_name} — {rand(total_cents)} from last week still outstanding.\n\n"
        "Leads pause Thursday if it's not settled. Not a threat, just how it works."
    )


def a12_paused(first_name: str, total_cents: int) -> str:
    return (
        f"Leads paused, {first_name}. {rand(total_cents)} outstanding.\n\n"
        "Settle and you're back on same day. Nothing else changes."
    )


def payment_received(amount_cents: int, restored: bool = False) -> str:
    if restored:
        return f"Got it. {rand(amount_cents)} cleared. Leads are back on."
    return f"Got it. {rand(amount_cents)} cleared. Thanks."


def credit_transition(
    first_name: str, credit_amount_cents: int, n_jobs: int, fee_cents: int, link: str
) -> str:
    return (
        f"{first_name} — three jobs done, all settled. Thanks for that.\n\n"
        f"From here it moves to credit: load {rand(credit_amount_cents)}, it covers your next "
        f"{n_jobs} jobs, debits when you take one. No more Friday invoices.\n\n"
        f"Top up whenever. Same {rand(fee_cents)} per job, nothing changes on your side "
        "except the timing.\n\n"
        f"Load here: {link}"
    )


# ───────────────────────── Customer side ─────────────────────────

def b1_confirm_logged(first_name: str, plumber_first: str, keyword: str, value_cents: int) -> str:
    return (
        f"{first_name} — {plumber_first} logged the {keyword} job at {rand(value_cents)}.\n\n"
        "Sound right?"
    )


B1_BUTTONS = [("yes", "Yes"), ("off", "Something's off")]


def b2_did_it_get_sorted(first_name: str, plumber_first: str, keyword: str) -> str:
    return f"{first_name} — did {plumber_first} get the {keyword} sorted?\n\nOne word is fine."


def b3_thanks(first_name: str) -> str:
    return (
        f"Good. Thanks {first_name}.\n\n"
        "If you ever need a plumber again, just message this number. No new forms."
    )


def b4_whats_off() -> str:
    return "Tell me what's off — price, work, or something else?"


def b5_never_came() -> str:
    return "That's not okay. Nothing should have been charged to you.\n\nDid anyone come out at all?"


def b6_price_flagged(plumber_first: str, logged_cents: int, actual_cents: int | None) -> str:
    if actual_cents is not None:
        said = f"you're saying {rand(actual_cents)}, {plumber_first} logged {rand(logged_cents)}"
    else:
        said = f"the price wasn't {rand(logged_cents)}, which is what {plumber_first} logged"
    return (
        f"Noted — {said}.\n\n"
        "I'll take it up with him. Doesn't change anything on your side.\n\n"
        "Thanks for flagging it."
    )


def customer_ack() -> str:
    return "Got it. Thanks."
