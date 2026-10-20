"""Reading a customer's answer to "how urgent is it?".

The tier decides the ETA promised and whether the flood advice is sent, so a wrong
answer is felt. This only answers when the message is short and says exactly one
thing: "now", "today", "this week", a quote. Anything else (negations like "not
urgent", two different answers, a long message, a time it does not know like
"tomorrow morning") returns None and goes to the model, which reads the whole
message. Keyword matching alone read "not urgent" as an emergency.
"""

from __future__ import annotations

import re

EMERGENCY = "Emergency — right now"
TODAY = "Today"
THIS_WEEK = "This week"
QUOTE = "Just a quote"

_TIERS = {
    EMERGENCY: re.compile(r"\b(?:right now|now|asap|immediately|urgent(?:ly)?|emergency)\b", re.I),
    TODAY: re.compile(
        r"\b(?:today|tonight|this (?:morning|afternoon|evening)|later today|few hours)\b", re.I),
    THIS_WEEK: re.compile(r"\b(?:this week|next week|few days|weekend)\b", re.I),
    QUOTE: re.compile(r"\b(?:quote|quotation|estimate|price|pricing|cost|how much)\b", re.I),
}

# "not urgent", "isn't an emergency", "no rush", "no hurry", "whenever": relaxed, so a week.
_RELAXED = re.compile(
    r"\b(?:not|isn['’]?t|aren['’]?t|no)\s+(?:(?:very|that|really|so|too)\s+)?"
    r"(?:urgent|an emergency|emergency|in a hurry|in a rush|a rush|a hurry|rushed)\b"
    r"|\bno (?:rush|hurry)\b|\bwhenever\b|\bany ?time\b",
    re.I,
)
# Any other negation can flip a keyword ("not today", "can't do now"): hand it to the model.
_NEGATION = re.compile(r"\b(?:not|never|no|nope|can['’]?t|cannot|don['’]?t|won['’]?t|isn['’]?t|wasn['’]?t)\b|n['’]t\b", re.I)

MAX_WORDS = 12


def parse_urgency_text(text: str) -> str | None:
    """One of the four urgency labels, or None when the model should decide."""
    if len(text.split()) > MAX_WORDS:
        return None

    matched: set[str] = set()
    rest = text
    if _RELAXED.search(rest):
        matched.add(THIS_WEEK)
        rest = _RELAXED.sub(" ", rest)
    if _NEGATION.search(rest):
        return None
    for label, pattern in _TIERS.items():
        if pattern.search(rest):
            matched.add(label)
    return matched.pop() if len(matched) == 1 else None
