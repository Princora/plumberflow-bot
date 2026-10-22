"""WhatsApp Cloud API adapter. NOT tested against Meta; the request shapes follow the
Cloud API docs and everything is injectable so you can test it against your own number.

Two things Meta enforces that you must plan for:
* Free text only works inside 24 hours of the recipient's last message to you. Outside
  that window you need an approved template. CloudMessenger sends free text first and,
  if Meta refuses it, falls back to `fallback_template` with the text as {{1}}. Create
  and get that template approved in Meta Business Manager before relying on it.
* Interactive buttons (B1) are also window-bound.
"""

from __future__ import annotations

import json
import logging
import re
import urllib.error
import urllib.request
from typing import Callable, Iterator

from .phone import normalize_phone

logger = logging.getLogger(__name__)


def _param(value) -> str:
    """Meta rejects template values with line breaks, tabs or runs of spaces, and empty values."""
    text = re.sub(r"\s+", " ", str(value)).strip()
    return text[:1000] or "-"


def _post(url: str, token: str, payload: dict) -> dict:
    req = urllib.request.Request(url, data=json.dumps(payload).encode(), headers={
        "Authorization": f"Bearer {token}", "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        raise WhatsAppError(e.code, e.read().decode(errors="replace")) from e


class WhatsAppError(Exception):
    def __init__(self, status: int, body: str):
        super().__init__(f"{status}: {body}")
        self.status, self.body = status, body


class WhatsAppCloud:
    def __init__(self, token: str, phone_number_id: str, *, post: Callable = _post, version: str = "v21.0"):
        self.token, self._post = token, post
        self.url = f"https://graph.facebook.com/{version}/{phone_number_id}/messages"

    def _send(self, to: str, body: dict) -> dict:
        return self._post(self.url, self.token, {
            "messaging_product": "whatsapp", "to": normalize_phone(to), **body})

    def send_text(self, to: str, text: str) -> dict:
        return self._send(to, {"type": "text", "text": {"body": text}})

    def send_template(self, to: str, name: str, params: list, lang: str = "en",
                      quick_replies: list[str] | None = None) -> dict:
        """quick_replies: one payload per quick-reply button on the template, in order."""
        components = [{"type": "body", "parameters": [{"type": "text", "text": _param(p)} for p in params]}]
        for i, payload in enumerate(quick_replies or []):
            components.append({"type": "button", "sub_type": "quick_reply", "index": str(i),
                               "parameters": [{"type": "payload", "payload": payload}]})
        return self._send(to, {"type": "template", "template": {
            "name": name, "language": {"code": lang}, "components": components}})

    def send_buttons(self, to: str, text: str, buttons: list[tuple[str, str]]) -> dict:
        return self._send(to, {"type": "interactive", "interactive": {
            "type": "button", "body": {"text": text},
            "action": {"buttons": [{"type": "reply", "reply": {"id": i, "title": t[:20]}}
                                   for i, t in buttons[:3]]}}})


class CloudMessenger:
    """Implements verification.Messenger on top of WhatsAppCloud + Telegram.

    Free text goes first. If Meta refuses it (outside the 24-hour window), a message that knows its
    own approved template (verification.templates.Templated) is sent as that template; anything
    else, or a template Meta refuses, falls back to the catch-all `fallback_template` when one is set.
    """

    def __init__(self, wa: WhatsAppCloud, telegram, *, phone_column: str = "whatsapp",
                 fallback_template: str | None = None, template_lang: str = "en"):
        self.wa, self.tg, self.col, self.fallback = wa, telegram, phone_column, fallback_template
        self.lang = template_lang or "en"

    def send_text(self, phone: str, text: str) -> None:
        try:
            self.wa.send_text(phone, text)
            return
        except WhatsAppError as refused:
            error = refused
        name = getattr(text, "template", None)
        if name:
            try:
                self.wa.send_template(phone, name, text.params, self.lang)
                return
            except WhatsAppError as e:
                logger.warning("template %s was refused (%s)", name, e)
                error = e
        if not self.fallback:
            raise error
        self.wa.send_template(phone, self.fallback, [str(text).replace("\n", " ")], self.lang)

    _text = send_text

    def send_plumber(self, plumber: dict, text: str) -> None:
        self.send_text(plumber[self.col], text)

    def send_customer(self, phone: str, text: str) -> None:
        self.send_text(phone, text)

    def send_customer_buttons(self, phone, text, buttons, job_id) -> None:
        # The job id travels in the button id so the tap can be routed back.
        payloads = [f"{job_id}|{bid}" for bid, _ in buttons]
        try:
            self.wa.send_buttons(phone, text, [(f"{job_id}|{bid}", title) for bid, title in buttons])
        except WhatsAppError:
            name = getattr(text, "template", None)
            if not name:
                raise
            # Window closed: the same question as a template with the same two quick-reply buttons.
            self.wa.send_template(phone, name, text.params, self.lang, quick_replies=payloads)

    def notify_founder(self, text: str) -> None:
        self.tg.notify(text)


def extract_messages(webhook: dict) -> Iterator[dict]:
    """Flatten a Cloud API webhook into {'phone', 'kind': 'text'|'button', ...}."""
    for entry in webhook.get("entry", []):
        for change in entry.get("changes", []):
            for m in change.get("value", {}).get("messages", []):
                phone = m.get("from", "")
                if m.get("type") == "text":
                    yield {"phone": phone, "kind": "text", "text": m["text"]["body"]}
                elif m.get("type") == "interactive":
                    reply = m["interactive"].get("button_reply")
                    if reply:
                        yield {"phone": phone, "kind": "button", "id": reply["id"], "title": reply.get("title", "")}
                elif m.get("type") == "button":
                    # A tap on a template's quick-reply button; the payload is what we attached.
                    tap = m.get("button") or {}
                    if tap.get("payload"):
                        yield {"phone": phone, "kind": "button", "id": tap["payload"], "title": tap.get("text", "")}
