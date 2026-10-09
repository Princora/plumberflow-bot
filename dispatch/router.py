"""Glue: one function for your WhatsApp webhook."""

from __future__ import annotations

from typing import Callable

from .whatsapp import extract_messages


def handle_whatsapp_webhook(
    payload: dict, dispatch, verification, *,
    on_customer_text: Callable[[str, str], None],
    on_plumber_unhandled: Callable[[str, str], None] | None = None,
) -> None:
    """
    Plumber text   -> take (dispatch), then done / no go / amounts (verification).
    Button tap     -> verification.on_customer_button(job_id, button_id) for B1 taps.
    Anything else  -> on_customer_text(phone, text): your existing bot (Haiku NLU) handles it.
    """
    for m in extract_messages(payload):
        if m["kind"] == "button":
            job_id, sep, button_id = m["id"].partition("|")
            if sep:
                verification.on_customer_button(job_id, button_id)
            continue
        if dispatch.on_whatsapp_text(m["phone"], m["text"]):
            continue
        if dispatch.find_plumber_by_phone(m["phone"]):
            if on_plumber_unhandled:       # e.g. forward to you on Telegram
                on_plumber_unhandled(m["phone"], m["text"])
            continue
        on_customer_text(m["phone"], m["text"])
