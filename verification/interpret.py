"""Model-assisted reading of plumber replies.

The deterministic parsers in parsing.py decide whenever they are sure. This is
only asked when they are not: a messy amount ("R2k", "two thousand four hundred",
two numbers in one message) or an unclear yes/no. It never writes a message to
anyone and never changes a job. It returns a label or a number, which the
service validates, and anything the service cannot confirm still goes back to the
plumber or to the founder. Every failure returns None: the caller falls back to
asking, exactly as it does with no model configured.
"""

from __future__ import annotations

import json
import logging
from typing import Protocol

from .parsing import MAX_JOB_VALUE_CENTS, MIN_JOB_VALUE_CENTS

logger = logging.getLogger(__name__)

MODEL = "claude-haiku-4-5"

AGREEMENT_LABELS = ("agrees", "disputes", "unclear")


class ReplyInterpreter(Protocol):
    def agreement(self, text: str, question: str) -> str | None:
        """'agrees' | 'disputes' | 'unclear', or None if no answer could be had."""

    def job_value(self, text: str) -> int | None:
        """What the plumber says they charged, in cents, or None."""


_AGREEMENT_SCHEMA = {
    "type": "object",
    "properties": {"label": {"type": "string", "enum": list(AGREEMENT_LABELS)}},
    "required": ["label"],
    "additionalProperties": False,
}

_AGREEMENT_SYSTEM = """You read a WhatsApp reply from a plumber in Johannesburg (South African English, sometimes Zulu or Afrikaans words like "yebo", "ja", "nee").
The plumber was asked a question, given below. Decide what the reply means.

- agrees: they confirm it is right, or accept it.
- disputes: they say it is wrong, deny it, or push back.
- unclear: you cannot tell, the reply is about something else, or it is a bare "no" that could be answering either way.

When unsure, answer unclear. Return ONLY valid JSON matching the schema."""

_VALUE_SCHEMA = {
    "type": "object",
    "properties": {
        "amount_rand": {
            "anyOf": [{"type": "number"}, {"type": "null"}],
            "description": "The single total price the plumber says they charged for the job, in rand. Null if there is no clear single price.",
        },
    },
    "required": ["amount_rand"],
    "additionalProperties": False,
}

_VALUE_SYSTEM = """You read a WhatsApp message from a plumber in Johannesburg who has finished a job and is saying what they charged.
Extract the single total price in rand.

- "R2k", "2k" and "two thousand four hundred" are R2000, R2000 and R2400.
- Ignore hours, minutes, counts, dates and phone numbers.
- If the message gives parts and a total, use the total. If it gives several prices and no total, or no price at all, return null.
- Never add numbers together or guess. When unsure, return null.

Return ONLY valid JSON matching the schema."""


class ClaudeReplyInterpreter:
    def __init__(self, client, model: str = MODEL) -> None:
        self._client = client   # a sync anthropic.Anthropic; the service runs off the event loop
        self._model = model

    def _ask(self, system: str, schema: dict, user: str, max_tokens: int) -> dict | None:
        try:
            response = self._client.messages.create(
                model=self._model,
                max_tokens=max_tokens,
                system=[{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
                messages=[{"role": "user", "content": user}],
                output_config={"format": {"type": "json_schema", "schema": schema}},
            )
            block = next((b.text for b in response.content if b.type == "text"), None)
            return json.loads(block) if block else None
        except Exception as exc:  # network, quota, bad JSON: all mean "no answer"
            logger.warning("reply interpretation failed: %s", exc)
            return None

    def agreement(self, text: str, question: str) -> str | None:
        result = self._ask(
            _AGREEMENT_SYSTEM, _AGREEMENT_SCHEMA,
            f"Question the plumber was asked: {question}\nTheir reply: {text}", 20,
        )
        label = (result or {}).get("label")
        return label if label in AGREEMENT_LABELS else None

    def job_value(self, text: str) -> int | None:
        result = self._ask(_VALUE_SYSTEM, _VALUE_SCHEMA, text, 40)
        amount = (result or {}).get("amount_rand")
        if isinstance(amount, bool) or not isinstance(amount, (int, float)):
            return None
        cents = round(amount * 100)
        return cents if MIN_JOB_VALUE_CENTS <= cents <= MAX_JOB_VALUE_CENTS else None
