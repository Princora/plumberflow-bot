"""Deterministic parsing of plumber replies.

Claude Haiku can sit in front of this for fuzzy intent; these functions are the
fallback and the contract. Outbound text never comes from the model.
"""

from __future__ import annotations

import re

_DONE = re.compile(r"^\s*(done|job done|finished|completed|sorted)\b", re.I)
_NO_GO = re.compile(r"^\s*(no[\s_-]?go|nogo|fell through|cancell?ed|cancel)\b", re.I)
_AMOUNT = re.compile(r"(\d{1,3}(?:[ ,]\d{3})+(?:\.\d{1,2})?|\d+(?:\.\d{1,2})?)")
_A9_ITEM = re.compile(
    r"(\d+)\s*[:.\-)]?\s*(done|no[\s_-]?go|nogo)(?:\s*r?\s*(\d{1,3}(?:[ ,]\d{3})+(?:\.\d{1,2})?|\d+(?:\.\d{1,2})?))?",
    re.I,
)
_WRONG = re.compile(r"\b(wrong|incorrect|not right|no|nope|didn.?t)\b", re.I)

MIN_JOB_VALUE_CENTS = 50 * 100       # "done, 3 hours" is not R3
MAX_JOB_VALUE_CENTS = 500_000 * 100


def parse_intent(text: str) -> str | None:
    """'done' | 'no_go' | None."""
    if _NO_GO.match(text):
        return "no_go"
    if _DONE.match(text):
        return "done"
    return None


def parse_amount_cents(text: str) -> int | None:
    """'R2400', '2 400', '2,400', 'r2400.50' -> cents. None if no sane amount."""
    m = _AMOUNT.search(text)
    if not m:
        return None
    raw = m.group(1).replace(" ", "").replace(",", "")
    try:
        cents = round(float(raw) * 100)
    except ValueError:
        return None
    if cents < MIN_JOB_VALUE_CENTS or cents > MAX_JOB_VALUE_CENTS:
        return None
    return cents


def parse_choice(text: str) -> int | None:
    """A bare number picking an option from a numbered list."""
    m = re.fullmatch(r"\s*(\d{1,2})\s*[.)]?\s*", text)
    return int(m.group(1)) if m else None


def parse_a9_reply(text: str) -> list[tuple[int, str, int | None]]:
    """'1 done R2400, 2 no go' -> [(1, 'done', 240000), (2, 'no_go', None)]."""
    items: list[tuple[int, str, int | None]] = []
    for num, what, amount in _A9_ITEM.findall(text):
        claim = "done" if what.lower() == "done" else "no_go"
        cents = parse_amount_cents(amount) if amount else None
        items.append((int(num), claim, cents))
    return items


def is_wrong(text: str) -> bool:
    """Plumber disputing a day-4 charge notice."""
    return bool(_WRONG.search(text))
