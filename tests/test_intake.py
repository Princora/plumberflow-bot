import pytest

from intake import EMERGENCY, QUOTE, THIS_WEEK, TODAY, parse_urgency_text


@pytest.mark.parametrize("text,tier", [
    ("it's an emergency", EMERGENCY),
    ("asap please", EMERGENCY),
    ("right now!", EMERGENCY),
    ("need it now", EMERGENCY),
    ("today", TODAY),
    ("tonight", TODAY),
    ("this afternoon", TODAY),
    ("this week", THIS_WEEK),
    ("few days", THIS_WEEK),
    ("how much does a geyser cost", QUOTE),
    ("just a quote", QUOTE),
    # Negations used to be read as emergencies.
    ("not urgent", THIS_WEEK),
    ("it's not an emergency", THIS_WEEK),
    ("isn't urgent", THIS_WEEK),
    ("Not very urgent", THIS_WEEK),
    ("no rush", THIS_WEEK),
    ("whenever", THIS_WEEK),
])
def test_clear_answers(text, tier):
    assert parse_urgency_text(text) == tier


@pytest.mark.parametrize("text", [
    "can someone come tomorrow morning",             # a time it does not know
    "within the hour please",
    "burst pipe flooding the kitchen",               # "burst" alone no longer decides
    "not today",                                     # a negation next to a keyword
    "urgent quote please",                           # two different answers
    "no rush, just need a quote",
    "I just need to know how much, it's not an emergency",
    "I don't know",
    "no",
    "",
    "it has been dripping for a while and my wife says it is getting worse today so can you come",
])
def test_everything_else_goes_to_the_model(text):
    assert parse_urgency_text(text) is None


# ───────────────────────── job type ─────────────────────────

from intake import BLOCKED, BURST, GEYSER, LEAK, OTHER, parse_job_type_text


@pytest.mark.parametrize("text,job", [
    ("burst pipe", BURST),
    ("pipe burst outside", BURST),
    ("blocked", BLOCKED),
    ("blocked toilet", BLOCKED),
    ("I need a blockage cleared", BLOCKED),
    ("drain", BLOCKED),
    ("geyser", GEYSER),
    ("no hot water", GEYSER),                        # a symptom, not a negation
    ("leak", LEAK),
    ("a leaking tap", LEAK),
    ("my toilet is leaking", LEAK),                  # was "Blocked drain"
    ("tap won't stop dripping", LEAK),
    ("other", OTHER),
    ("something else", OTHER),
    ("general", OTHER),
])
def test_clear_job_types(text, job):
    assert parse_job_type_text(text) == job


@pytest.mark.parametrize("text", [
    "running toilet",                                # "toilet" alone is not a type
    "toilet won't flush",
    "overflowing toilet",
    "the drain isn't blocked but smells",            # was "Blocked drain"
    "not a geyser problem, it's the tap",            # was "Geyser problem"
    "my geyser is fine but the drain is blocked",
    "geyser burst",                                  # two types: the model picks the source
    "burst geyser flooding the garage",              # was "Burst pipe"
    "the geyser is leaking",
    "no hot water and the pipe is leaking",
    "flooded after the rain",                        # was "Burst pipe"
    "water everywhere",
    "water coming through the ceiling",
    "bathroom renovation",
    "",
    "my kitchen tap has been dripping for weeks and now the cupboard underneath is soaked through",
])
def test_everything_else_goes_to_the_model(text):
    assert parse_job_type_text(text) is None
