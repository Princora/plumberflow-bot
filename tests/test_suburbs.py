import pytest

from dispatch.suburbs import CANONICAL, area_key, display_name, resolve_suburb


@pytest.mark.parametrize("typed,key", [
    ("Randburg", "randburg"),
    ("rivonia.", "rivonia"),
    ("Rivonia Ext 3", "rivonia"),
    ("Rivonia extension 12", "rivonia"),
    ("Fourways, Johannesburg", "fourways"),
    ("  SANDTON  ", "sandton"),
    ("hyde park", "hyde park"),
    ("Hydepark", "hyde park"),
    ("Hyde-Park", "hyde park"),
    ("Bryanston East", "bryanston"),
    ("Sandton City", "sandton"),
    ("12 Main Rd, Sandton", "sandton"),
    ("Sandton, 12 Main Rd", "sandton"),
    ("I'm in Northcliff", "northcliff"),
    ("near Rivonia mall", "rivonia"),
    ("Midrand 1685", "midrand"),
    ("Fourwys", "fourways"),                 # a typo
    ("Parktown", "parktown north"),
    ("Montgomery Park", "montgomery park"),
])
def test_resolves_what_people_type(typed, key):
    assert resolve_suburb(typed) == key


@pytest.mark.parametrize("typed", [
    "", "   ", None,
    "Alexandra",                       # a real place, not on the list: never guessed at
    "joburg north",                    # a region
    "Between Sandton and Rivonia",     # two suburbs: ask
    "Sandtown",                        # too far from any one suburb to trust
])
def test_does_not_guess(typed):
    assert resolve_suburb(typed) is None


def test_neighbouring_names_are_never_confused():
    # Real, different suburbs with similar names stay separate.
    assert resolve_suburb("Sandown") == "sandown"
    assert resolve_suburb("Sandton") == "sandton"
    assert resolve_suburb("Sandhurst") == "sandhurst"
    assert resolve_suburb("Fairland") == "fairland"
    assert resolve_suburb("Fairlands") == "fairlands"


def test_every_listed_suburb_resolves_to_itself():
    for key, name in CANONICAL.items():
        assert resolve_suburb(name) == key
        assert resolve_suburb(name.upper() + ".") == key
        assert display_name(key) == name


def test_area_key_matches_across_spellings_and_keeps_unknown_places_comparable():
    assert area_key("Rivonia Ext 3") == area_key("rivonia") == "rivonia"
    assert area_key("Alexandra") == area_key(" alexandra. ") == "alexandra"
    assert area_key(None) == "" and area_key("") == ""
