"""Deterministic parsing of plumber replies.

These functions decide whenever they are sure, and they are the fallback and the
contract. Messy amounts and unclear yes/no answers can be handed to Claude Haiku
(see interpret.py), but its answer is validated and confirmed with the plumber
before anything is saved. Outbound text never comes from the model.
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
_NEGATED_DISPUTE = re.compile(r"\b(no|not|nothing to)\s+(dispute|issue|problem|complaint|worries)\b|\bnot disputing\b", re.I)
_DISPUTE = re.compile(
    r"\b(wrong|incorrect|inaccurate|false|dispute|contest|"
    r"(?:not|isn.?t|aren.?t|wasn.?t)\s+(?:right|correct|true|accurate|me|my)|"
    r"(?:didn.?t|did\s+not|never)\s+(?:do|go|come|attend|work|touch)|"
    r"never\s+(?:was|been)\s+there)\b",
    re.I,
)
_AGREE = re.compile(
    r"\b(right|correct|true|accurate|fine|good|ok|okay|yes|yebo|yep|yeah|ya|ja|agree|sorted|"
    r"all\s+good|no\s+problem|that.?s\s+it|thanks|thank\s+you|cool|sure)\b",
    re.I,
)

MIN_JOB_VALUE_CENTS = 50 * 100       # "done, 3 hours" is not R3
MAX_JOB_VALUE_CENTS = 500_000 * 100


def parse_intent(text: str) -> str | None:
    """'done' | 'no_go' | None."""
    if _NO_GO.match(text):
        return "no_go"
    if _DONE.match(text):
        return "done"
    return None


# One number as written: "2 400", "2,400", "2400.50", with an optional R prefix or k suffix.
_NUM = r"(?:\d{1,3}(?:[ ,]\d{3})+|\d+)(?:\.\d{1,2})?"
_NUMBER = re.compile(rf"(?P<r>\bR\s?)?(?P<num>{_NUM})(?P<k>\s?k\b)?", re.I)
# A bare number followed by one of these is a duration, count or date, not a price.
_NOT_MONEY_AFTER = re.compile(
    r"^\s*(?:min|mins|minutes?|hrs?|hours?|days?|weeks?|months?|years?|x\b|"
    r"geysers?|taps?|toilets?|pipes?|units?|jobs?|"
    r"jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)",
    re.I,
)
_YEAR = re.compile(r"^(?:19|20)\d\d$")


def _to_cents(num: str, k: bool) -> int | None:
    try:
        value = float(num.replace(" ", "").replace(",", "")) * (1000 if k else 1)
    except ValueError:
        return None
    cents = round(value * 100)
    return cents if MIN_JOB_VALUE_CENTS <= cents <= MAX_JOB_VALUE_CENTS else None


def parse_amount_cents(text: str) -> int | None:
    """'R2400', '2 400', '2,400', 'r2400.50', 'R2k' -> cents.

    Returns None unless the amount is unambiguous. Numbers marked with R win; an
    unmarked number counts only if it is the single plausible one (not a duration,
    count, date or year). Two different R amounts, or two unmarked numbers, are
    ambiguous: None, so the caller asks instead of guessing.
    """
    marked: list[int] = []
    unmarked: list[int] = []
    for m in _NUMBER.finditer(text):
        num, has_r, has_k = m.group("num"), bool(m.group("r")), bool(m.group("k"))
        if not has_r and not has_k:
            if _NOT_MONEY_AFTER.match(text[m.end():]) or _YEAR.match(num):
                continue
            # digits glued to letters ("3d", "x2") or part of a phone number are not prices
            if len(num.replace(" ", "")) >= 9:
                continue
        cents = _to_cents(num, has_k)
        if cents is None:
            continue
        (marked if has_r or has_k else unmarked).append(cents)
    pool = marked or unmarked
    return pool[0] if len(set(pool)) == 1 else None


_CLEAN_DONE = re.compile(
    rf"^\s*(?:job\s+)?(?:done|finished|completed|sorted)\b[\s,.:;!\-]*(?:R\s?)?{_NUM}\s*$", re.I
)


_BARE_AMOUNT = re.compile(rf"^\s*(?:R\s?)?{_NUM}\s*$", re.I)
_BARE_NO = re.compile(r"^\s*(?:no|nope|nah)[\s,.!]*$", re.I)


def is_bare_amount(text: str) -> bool:
    """A reply that is only an amount, e.g. the answer to "What did you charge?"."""
    return bool(_BARE_AMOUNT.match(text)) and parse_amount_cents(text) is not None


def is_clean_done_message(text: str) -> bool:
    """'done R2400' and nothing else. Anything wordier gets confirmed before it is logged."""
    return bool(_CLEAN_DONE.match(text)) and parse_amount_cents(text) is not None


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


def classify_agreement(text: str, *, bare_no: str = "unclear") -> str:
    """Plumber's answer to "is that right?": 'agrees' | 'disputes' | 'unclear'.

    Only clear signals decide. A bare "no" is unclear on purpose: after "if that's
    wrong, tell me" it can mean either, and guessing wrong either pulls a good
    charge or confirms a bad one. Unclear goes to the model, then to a human.
    Pass bare_no="disputes" when "no" can only mean "that's not right".
    """
    if _BARE_NO.match(text):
        return bare_no
    if _NEGATED_DISPUTE.search(text):
        return "agrees"
    if _DISPUTE.search(text):
        return "disputes"
    if _AGREE.search(text):
        return "agrees"
    return "unclear"


def is_wrong(text: str) -> bool:
    """Plumber disputing a day-4 charge notice."""
    return classify_agreement(text) == "disputes"
