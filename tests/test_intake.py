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
