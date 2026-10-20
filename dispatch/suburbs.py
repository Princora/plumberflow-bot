"""One spelling per suburb, so "Rivonia Ext 3", "rivonia." and "Rivonia" are the same place.

A customer types a suburb and a plumber types a list of suburbs. Matching them with
string equality missed anything but an exact spelling. resolve_suburb() turns either
side into a canonical key (the lowercase name, which is also how plumber areas are
already stored). It is pure and deterministic, so dispatch can use it on every lead.

Where it cannot tell, it returns None rather than guessing. The caller then asks the
person, or asks the model to pick from CANONICAL (it cannot invent a suburb that is
not on this list).

The list is the suburbs on plumberflow.co.za plus their "nearby areas", and a few
well-known neighbours. Add a suburb here and every part of the bot knows it.
"""

from __future__ import annotations

import difflib
import re

DISPLAY_NAMES = (
    "Atholl", "Auckland Park", "Benmore", "Berario", "Blackheath", "Blairgowrie", "Bordeaux",
    "Boskruin", "Bramley", "Bryanston", "Carlswald", "Constantia Kloof", "Craigavon",
    "Craighall", "Cresta", "Dainfern", "Darrenwood", "Discovery", "Douglasdale", "Dunkeld",
    "Edenburg", "Emmarentia", "Epsom Downs", "Fairland", "Fairlands", "Ferndale", "Florida",
    "Fourways", "Glen Austin", "Greenside", "Halfway House", "Honeydew", "Horizon",
    "Houghton", "Hyde Park", "Illovo", "Kyalami", "Linden", "Lonehill", "Magaliessig",
    "Melrose", "Melville", "Midrand", "Montgomery Park", "Morningside", "Noordwyk",
    "Norscot", "Northcliff", "Northriding", "Parkhurst", "Parktown North", "Parkview",
    "Paulshof", "Petervale", "Randburg", "Randpark Ridge", "Riverclub", "Rivonia",
    "Robindale", "Roodepoort", "Rosebank", "Sandhurst", "Sandown", "Sandton", "Soweto",
    "Strathavon", "Sunninghill", "Vorna Valley", "Waterfall", "Weltevreden Park",
    "Wendywood", "Westdene", "Windsor", "Witkoppen", "Woodmead",
)

CANONICAL: dict[str, str] = {name.lower(): name for name in DISPLAY_NAMES}

ALIASES = {
    "parktown": "parktown north",
    "sandton city": "sandton",
    "joburg north": None,   # a region, not a suburb: leave it to a person
}
ALIASES = {k: v for k, v in ALIASES.items() if v}

_PLACE_WORDS = r"(?:johannesburg|joburg|jhb|jozi|gauteng|south africa|za|sa)"
_EXTENSION = re.compile(r"\b(?:ext(?:ension|n)?\.?|x)\s*\d+\b", re.I)
_LEADING = re.compile(r"^(?:i\s*(?:am|'m)\s+)?(?:in|at|near|around|from|the)\s+", re.I)

# Typos only: high enough that Sandton and Sandown (two real suburbs) never collapse.
_FUZZY_CUTOFF = 0.88
_FUZZY_MARGIN = 0.05


def _clean(text: str) -> str:
    t = text.lower().replace("’", "'")
    t = _EXTENSION.sub(" ", t)
    t = re.sub(rf"\b{_PLACE_WORDS}\b", " ", t)
    t = re.sub(r"[^\w\s]", " ", t)
    return re.sub(r"\s+", " ", t).strip()


def _compact(text: str) -> str:
    return re.sub(r"[\s'\-]", "", text)


_COMPACT = {_compact(k): k for k in CANONICAL}


def _resolve_phrase(phrase: str) -> str | None:
    t = _clean(_LEADING.sub("", phrase.strip()))
    if not t:
        return None
    if t in CANONICAL:
        return t
    if t in ALIASES:
        return ALIASES[t]
    if _compact(t) in _COMPACT:                      # "hydepark", "hyde-park"
        return _COMPACT[_compact(t)]

    # A suburb named inside a longer answer: "Bryanston East", "Sandton City", "near Rivonia mall".
    found = {
        key for key in CANONICAL
        if re.search(rf"(?<!\w){re.escape(key)}(?!\w)", t)
    } | {
        target for alias, target in ALIASES.items()
        if re.search(rf"(?<!\w){re.escape(alias)}(?!\w)", t)
    }
    # "sandton" inside "parktown north" style overlaps: keep only the longest names.
    found = {k for k in found if not any(k != o and k in o for o in found)}
    if len(found) == 1:
        return found.pop()
    if len(found) > 1:
        return None                                  # "between Sandton and Rivonia": ask

    # A typo of a single suburb.
    scored = sorted(
        ((difflib.SequenceMatcher(None, _compact(t), c).ratio(), key) for c, key in _COMPACT.items()),
        reverse=True,
    )
    if scored and scored[0][0] >= _FUZZY_CUTOFF:
        if len(scored) == 1 or scored[0][0] - scored[1][0] >= _FUZZY_MARGIN:
            return scored[0][1]
    return None


def resolve_suburb(text: str | None) -> str | None:
    """Canonical key ("rivonia") for what someone typed, or None if it is not clearly one suburb.

    Tries the whole text, then each comma-separated part, last first, because an
    address reads "12 Main Rd, Sandton".
    """
    if not text:
        return None
    whole = _resolve_phrase(text)
    if whole:
        return whole
    parts = [p for p in re.split(r"[,;\n]", text) if p.strip()]
    if len(parts) > 1:
        hits = {h for h in (_resolve_phrase(p) for p in parts) if h}
        if len(hits) == 1:
            return hits.pop()
    return None


def display_name(key: str) -> str:
    return CANONICAL.get(key, key.title())


def area_key(text: str | None) -> str:
    """Comparison key for matching a lead to a plumber's areas.

    A recognised suburb gives its canonical key. Anything else (a place not on the
    list) still compares equal to itself after tidying, so plumbers who cover it
    keep matching exactly as before.
    """
    if not text:
        return ""
    return resolve_suburb(text) or _clean(text)
