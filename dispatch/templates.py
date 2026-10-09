"""Dispatch wording. Same voice rules as verification.templates: plain, no exclamation
marks, no marketing words. The offer text reuses A1 (lead_arrival) so there is one
voice from first offer to invoice."""

from verification import templates as VT


def offer_to_plumber(ref: str, suburb: str, job_short: str, n_viewing: int, fee_cents: int) -> str:
    return f"{VT.lead_arrival(suburb, job_short, n_viewing, fee_cents)}\n\nReply take to claim it · {ref}"


def which_offer(options: list[tuple[str, str]]) -> str:
    lines = "\n".join(f"{ref} · {label}" for ref, label in options)
    return f"You have more than one open.\n\n{lines}\n\nReply take and the code, for example take {options[0][0]}."


def gone(label: str) -> str:
    return f"{label} was taken before your reply came in."


def lost_to_other(label: str) -> str:
    return f"{label} has been taken by another plumber."


# ───── founder (Telegram) ─────

def founder_lead(lead: dict) -> str:
    where = lead.get("suburb") or "Suburb unknown"
    what = lead.get("summary") or lead.get("keyword") or "No description"
    who = " · ".join(x for x in (lead.get("customer_name"), lead.get("customer_phone")) if x)
    return f"{lead['ref']} · {where}\n{what}\n{who}"
