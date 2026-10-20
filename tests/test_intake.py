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


# ───────────────────────── onboarding: experience and PIRB ─────────────────────────

from datetime import date

from intake import parse_experience, parse_pirb, parse_years

TODAY = date(2026, 10, 20)


@pytest.mark.parametrize("text,years", [
    ("5", 5),
    ("5 years", 5),                  # was rejected
    ("5 yrs", 5),
    ("5yrs", 5),
    ("Five years", 5),
    ("twenty five years", 25),
    ("10+ years", 10),
    ("5-7 years", 5),                # the lower end
    ("5 years experience", 5),
    ("about 12 years now", 12),
    ("a year", 1),
    ("6 months", 0),
    ("less than a year", 0),
    ("since 2015", 11),
    ("5 years since 2015", 5),       # a stated number beats a "since" next to it
    ("5.", 5),
    ("5, PIRB12345", 5),
])
def test_years_in_plain_words(text, years):
    assert parse_years(text, TODAY) == years


@pytest.mark.parametrize("text", ["", "hello", "lots", "many", "yes", "1990", "250 years", "since 1850"])
def test_no_clear_years(text):
    assert parse_years(text, TODAY) is None


@pytest.mark.parametrize("text,registered,number", [
    ("5, PIRB12345", True, "PIRB 12345"),
    ("7, PIRB123", True, "PIRB 123"),
    ("5 years, PIRB 4411", True, "PIRB 4411"),
    ("15 years PIRB no. 55821", True, "PIRB 55821"),
    ("12 years, I'm registered with PIRB number 99812", True, "PIRB 99812"),
    ("5, 12345", True, "PIRB 12345"),
    ("3, No PIRB", False, None),
    ("5 years, not registered", False, None),
    ("20 years no pirb", False, None),
    ("3 years, applying for PIRB", False, None),
    ("5 years, pirb: none", False, None),
    ("5, yes", True, None),
    ("5 years, registered", True, None),
    # Not stated: the bot asks.
    ("5", None, None),
    ("5 years", None, None),
    ("10+ years", None, None),
    ("3 years, not sure about PIRB", None, None),
    # A year is not a PIRB number.
    ("since 2015", None, None),
    ("5 years since 2015", None, None),
    ("1990", None, None),
])
def test_pirb_in_the_same_message(text, registered, number):
    pirb = parse_experience(text, TODAY).pirb
    assert (pirb.registered, pirb.number) == (registered, number)


@pytest.mark.parametrize("text,registered,number", [
    ("NO", False, None),
    ("nope", False, None),
    ("not yet", False, None),
    ("PIRB12345", True, "PIRB 12345"),
    ("4411", True, "PIRB 4411"),
    ("123", True, "PIRB 123"),
    ("1990", True, "PIRB 1990"),             # asked directly, so a number is a number
    ("yes", True, None),
    ("yebo I'm registered", True, None),
    ("hmm", None, None),
    ("I don't know", None, None),
])
def test_pirb_asked_on_its_own(text, registered, number):
    pirb = parse_pirb(text, bare_min_digits=3, skip_years=False)
    assert (pirb.registered, pirb.number) == (registered, number)


# ───────────────────────── onboarding: first name ─────────────────────────

from intake import parse_first_name


@pytest.mark.parametrize("text,name", [
    ("Sipho", "Sipho"),
    ("  sipho  ", "Sipho"),
    ("LERATO", "Lerato"),
    ("Sipho Dlamini", "Sipho"),                       # the first name only
    ("my name is Sipho", "Sipho"),                    # was saved as "My"
    ("I'm Sipho", "Sipho"),                           # was saved as "I'M"
    ("Hi, I'm Sipho", "Sipho"),
    ("it's Thabo", "Thabo"),                          # was saved as "It'S"
    ("This is Lerato from Soweto", "Lerato"),         # was saved as "This"
    ("Thabo Mokoena the plumber", "Thabo"),
    ("Mr Dlamini", "Dlamini"),                        # was saved as "Mr"
    ("Dr Naidoo", "Naidoo"),
    ("Sipho!", "Sipho"),                              # was rejected
    ("Sipho 👷", "Sipho"),
    ("Nomvula-Grace", "Nomvula-Grace"),
    ("O'Brien", "O'Brien"),
    ("McDonald", "McDonald"),                         # capitals kept as typed
    ("Zoë", "Zoë"),
])
def test_first_name_in_plain_words(text, name):
    assert parse_first_name(text) == name


@pytest.mark.parametrize("text", [
    "hello", "hi", "yes", "ja", "ok", "no", "join", "test",     # were all accepted as names
    "123", "x", "", "my", "Mr", "😀", "R2400",
    "a" * 41,
])
def test_what_is_not_a_name_is_asked_again(text):
    assert parse_first_name(text) is None
