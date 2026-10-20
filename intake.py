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


# ───────────────────────── What is the problem? ─────────────────────────
#
# Same rule as urgency: answer only when the message is short and names exactly one
# kind of problem, with no negation in front of it. "toilet" alone is not enough
# (leaking, running, blocked and overflowing toilets are different jobs), and
# "flood" alone is not enough (rain floods; so do blocked drains). Everything
# else goes to the model, which reads the whole message.

BURST = "Burst pipe"
BLOCKED = "Blocked drain"
GEYSER = "Geyser problem"
LEAK = "Leak"
OTHER = "Other"

_JOB_TYPES = {
    BURST: re.compile(r"\bburst\b", re.I),
    BLOCKED: re.compile(
        r"\b(?:blocked|blockage|blockages|clogged|drains?|sewage|(?:backing|backed) up)\b", re.I),
    GEYSER: re.compile(r"\b(?:no hot water|no hot|hot water|geysers?|heater|boiler)\b", re.I),
    LEAK: re.compile(r"\b(?:leaks?|leaking|leaked|drips?|dripping|seeping|seeps?)\b", re.I),
    OTHER: re.compile(r"\b(?:other|general|something else|misc)\b", re.I),
}

# A negator within three words before the problem word: "isn't blocked", "not a geyser problem".
_NEGATOR_BEFORE = re.compile(
    r"\b(?:not|no|isn['’]?t|aren['’]?t|wasn['’]?t|without|never|nothing)\b(?:\s+\w+){0,2}\s*$", re.I)


def parse_job_type_text(text: str) -> str | None:
    """One of the five job types, or None when the model should decide."""
    if len(text.split()) > MAX_WORDS:
        return None

    matched: set[str] = set()
    for label, pattern in _JOB_TYPES.items():
        for m in pattern.finditer(text):
            # "no hot water" is the symptom itself, not a negation of hot water.
            if not m.group(0).lower().startswith("no hot") and _NEGATOR_BEFORE.search(text[:m.start()]):
                return None
            matched.add(label)
    return matched.pop() if len(matched) == 1 else None


# ───────────────────────── Plumber onboarding: experience and PIRB ─────────────────────────
#
# "5, PIRB12345" used to be the only accepted shape. People write "5 years", "5 yrs",
# "five years, PIRB 12345", "10+ years", "since 2015" or "6 months". Whatever is read
# here is shown back in the sign-up summary, and the plumber confirms it with YES, so a
# misread is caught before it is saved. Nothing here verifies a PIRB number.

from dataclasses import dataclass
from datetime import date

MAX_EXPERIENCE_YEARS = 60

_NUMBER_WORDS = {
    "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8,
    "nine": 9, "ten": 10, "eleven": 11, "twelve": 12, "fifteen": 15, "twenty": 20,
    "twenty five": 25, "thirty": 30, "forty": 40,
}
_SINCE_YEAR = re.compile(r"\bsince\s+((?:19|20)\d\d)\b", re.I)
_EXPLICIT_YEARS = re.compile(r"(?<![\w.])(\d{1,2})(?!\d)\s*(?:\+|-\s*\d{1,2})?\s*(?:years?|yrs?)\b")
_LESS_THAN_A_YEAR = re.compile(r"\b(?:less than (?:a|1|one) year|under (?:a|1|one) year|just started|new to)\b", re.I)
_MONTHS_ONLY = re.compile(r"\bmonths?\b", re.I)
_YEARS_WORD = re.compile(r"\b(?:years?|yrs?)\b", re.I)
_DIGITS_YEARS = re.compile(r"(?<![\w.])(\d{1,2})(?!\d)\s*(?:\+|-\s*\d{1,2})?(?![\w])")
_A_YEAR = re.compile(r"\b(?:a|one) (?:year|yr)\b", re.I)


def parse_years(text: str, today: date | None = None) -> int | None:
    """Whole years of experience, or None if there is no clear number (or it is not plausible)."""
    t = text.lower()
    today = today or date.today()

    if _LESS_THAN_A_YEAR.search(t) or (_MONTHS_ONLY.search(t) and not _YEARS_WORD.search(t)):
        return 0
    m = _EXPLICIT_YEARS.search(t)
    if m:
        years = int(m.group(1))                          # "5 years" beats a "since 2015" next to it
    elif _SINCE_YEAR.search(t):
        years = today.year - int(_SINCE_YEAR.search(t).group(1))
    elif _A_YEAR.search(t):
        years = 1
    else:
        m = _DIGITS_YEARS.search(t)
        if m:
            years = int(m.group(1))                      # "5-7 years" reads as the lower number
        else:
            years = next((n for w, n in sorted(_NUMBER_WORDS.items(), key=lambda kv: -len(kv[0]))
                          if re.search(rf"\b{w}\b", t)), None)
    return years if years is not None and 0 <= years <= MAX_EXPERIENCE_YEARS else None


@dataclass(frozen=True)
class PirbAnswer:
    registered: bool | None          # None: the reply did not say
    number: str | None = None        # "PIRB 12345", exactly as given, never checked


_PIRB_NUMBER = re.compile(r"\bpirb\s*(?:no\.?|number|nr|#|:|-)?\s*([a-z]{0,3}\d+[a-z0-9]*)\b", re.I)
_NOT_REGISTERED = re.compile(
    r"\b(?:no|nope|nee|none|not|never|applying|applied|pending|busy|soon)\b|n['’]t\b", re.I)
_REGISTERED = re.compile(r"\b(?:yes|yebo|ja|yep|yeah|registered|i am|i'm)\b", re.I)


_YEAR_LIKE = re.compile(r"^(?:19|20)\d\d$")
_UNSURE = re.compile(r"\b(?:not sure|unsure|dont know|don['’]t know|no idea|what is)\b", re.I)


def parse_pirb(text: str, *, bare_min_digits: int = 4, skip_years: bool = True) -> PirbAnswer:
    """Registered with PIRB? A number wins; then an explicit no; then an explicit yes.

    A bare number (no "PIRB" in front of it) counts only if it has bare_min_digits digits,
    and, with skip_years, is not a year or the year after "since": "5 years since 2015"
    is not a PIRB number. When PIRB is being asked on its own, pass skip_years=False.
    """
    m = _PIRB_NUMBER.search(text)
    if not m:
        for lone in re.finditer(rf"(?<![\w.])(\d{{{bare_min_digits},}}[a-z0-9]*)(?!\w)", text, re.I):
            token = lone.group(1)
            if skip_years and (_YEAR_LIKE.match(token) or re.search(r"since\s*$", text[:lone.start()], re.I)):
                continue
            m = lone
            break
    if m:
        return PirbAnswer(True, f"PIRB {m.group(1).upper()}")
    if _UNSURE.search(text):
        return PirbAnswer(None)
    if _NOT_REGISTERED.search(text):
        return PirbAnswer(False)
    if _REGISTERED.search(text):
        return PirbAnswer(True)
    return PirbAnswer(None)


@dataclass(frozen=True)
class Experience:
    years: int | None
    pirb: PirbAnswer


def parse_experience(text: str, today: date | None = None) -> Experience:
    return Experience(parse_years(text, today), parse_pirb(text))
