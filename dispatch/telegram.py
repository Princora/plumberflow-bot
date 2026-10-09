"""Telegram adapter: the Bot API calls the dispatch service needs, plus the update router.

Only taps from your own Telegram user id are honoured. Anyone else who finds the bot
and presses a button gets nothing.
"""

from __future__ import annotations

import json
import urllib.request
from typing import Callable


def _post_json(url: str, payload: dict) -> dict:
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode(), headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.loads(r.read())


class TelegramClient:
    def __init__(self, token: str, chat_id: int | str, *, post: Callable[[str, dict], dict] = _post_json):
        self._base = f"https://api.telegram.org/bot{token}"
        self.chat_id, self._post = chat_id, post

    def _markup(self, buttons):
        return {"inline_keyboard": buttons} if buttons else {"inline_keyboard": []}

    def post(self, text: str, buttons=None) -> int:
        r = self._post(f"{self._base}/sendMessage", {
            "chat_id": self.chat_id, "text": text, "reply_markup": self._markup(buttons)})
        return r["result"]["message_id"]

    def edit(self, message_id: int, text: str, buttons=None) -> None:
        try:
            self._post(f"{self._base}/editMessageText", {
                "chat_id": self.chat_id, "message_id": message_id, "text": text,
                "reply_markup": self._markup(buttons)})
        except Exception:      # "message is not modified" and similar are harmless
            pass

    def answer_callback(self, callback_id: str, text: str = "") -> None:
        self._post(f"{self._base}/answerCallbackQuery",
                   {"callback_query_id": callback_id, "text": text})

    def notify(self, text: str) -> None:
        self._post(f"{self._base}/sendMessage", {"chat_id": self.chat_id, "text": text})


def handle_telegram_update(update: dict, dispatch, client: TelegramClient, *, founder_user_id: int) -> bool:
    """Feed every update from your Telegram webhook through this. True if it was a button tap."""
    cb = update.get("callback_query")
    if not cb:
        return False
    if cb.get("from", {}).get("id") != founder_user_id:
        return False
    toast = dispatch.on_telegram_callback(cb.get("data", ""))
    client.answer_callback(cb["id"], toast)
    return True
