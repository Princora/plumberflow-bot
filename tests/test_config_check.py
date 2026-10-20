import tomllib
from pathlib import Path

import pytest

from config_check import ConfigError, check_environment, report, require_valid_environment

GOOD = {
    "DATABASE_URL": "postgresql://app_user:s3cretpw@ep-quiet-night-pooler.eu-central-1.aws.neon.tech/neondb?sslmode=require",
    "WHATSAPP_TOKEN": "EAAB" + "x" * 40,
    "PHONE_NUMBER_ID": "109876543210987",
    "TELEGRAM_BOT_TOKEN": "123456789:" + "A" * 35,
    "TELEGRAM_CHAT_ID": "123456789",
    "FOUNDER_USER_ID": "123456789",
    "TELEGRAM_WEBHOOK_SECRET": "a-long-random-secret_123",
}


def problems_for(**changes):
    env = {**GOOD, **changes}
    env = {k: v for k, v in env.items() if v is not None}
    return check_environment(env)[0]


def test_a_correct_setup_has_no_problems():
    assert check_environment(GOOD) == ([], [])


def test_the_old_secret_names_are_accepted():
    new_names = {"WA_TOKEN": GOOD["WHATSAPP_TOKEN"], "WA_PHONE_NUMBER_ID": GOOD["PHONE_NUMBER_ID"],
                 "TELEGRAM_TOKEN": GOOD["TELEGRAM_BOT_TOKEN"], "FOUNDER_CHAT_ID": GOOD["TELEGRAM_CHAT_ID"]}
    old = {k: v for k, v in GOOD.items() if k not in ("WHATSAPP_TOKEN", "PHONE_NUMBER_ID", "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID")}
    assert check_environment({**old, **new_names})[0] == []


# The two mistakes that took the live bot down, with made-up values.
def test_the_pasted_psql_command_is_named():
    (p,) = problems_for(DATABASE_URL="<psql 'postgresql://neondb_owner:hunter2@ep-x-pooler.aws.neon.tech/neondb?sslmode=require'>")
    assert p.startswith("DATABASE_URL") and "psql command" in p and "connection string" in p


def test_the_telegram_id_with_extra_text_is_named():
    (p,) = problems_for(FOUNDER_USER_ID="< 123456789 --stage")
    assert p.startswith("FOUNDER_USER_ID") and "digits only" in p
    assert "spaces" in p and "--stage" in p


def test_every_problem_is_reported_at_once():
    found = problems_for(DATABASE_URL="psql 'postgresql://x'", FOUNDER_USER_ID="abc", TELEGRAM_BOT_TOKEN=None, TELEGRAM_CHAT_ID="x y")
    names = [p.split()[0] for p in found]
    assert names == ["DATABASE_URL", "TELEGRAM_TOKEN", "TELEGRAM_CHAT_ID", "FOUNDER_USER_ID"]


@pytest.mark.parametrize("name", ["DATABASE_URL", "WHATSAPP_TOKEN", "PHONE_NUMBER_ID", "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID", "FOUNDER_USER_ID"])
def test_a_missing_required_secret_is_named(name):
    (p,) = problems_for(**{name: None})
    assert "is not set" in p


@pytest.mark.parametrize("name,value", [
    ("DATABASE_URL", "mysql://u:p@host/db"),
    ("DATABASE_URL", "postgresql://"),
    ("DATABASE_URL", "postgresql://u:p@host/db "),
    ("DATABASE_URL", '"postgresql://u:p@host/db"'),
    ("WHATSAPP_TOKEN", "EAAB with spaces"),
    ("PHONE_NUMBER_ID", "not-a-number"),
    ("TELEGRAM_BOT_TOKEN", "not a token"),
    ("TELEGRAM_CHAT_ID", "12.5"),
    ("FOUNDER_USER_ID", ""),
    ("FOUNDER_USER_ID", "'123456789'"),
])
def test_values_with_something_wrong_are_rejected(name, value):
    env = {**GOOD, name: value}
    if value == "":
        env.pop(name)
    found = check_environment(env)[0]
    assert found and found[0].split()[0] == name


@pytest.mark.parametrize("name,value", [
    ("ADMIN_PHONE", "27821234567"), ("ADMIN_PHONE", "+27 82 123 4567"), ("TRIAL_LEADS", "0"), ("TRIAL_LEADS", "5"),
    ("WA_FALLBACK_TEMPLATE", "job_offer_v1"), ("TELEGRAM_CHAT_ID", "-1001234567890"),
])
def test_valid_optional_and_edge_values_pass(name, value):
    assert problems_for(**{name: value}) == []


@pytest.mark.parametrize("name,value", [
    ("ADMIN_PHONE", "call me"), ("TRIAL_LEADS", "five"), ("TRIAL_LEADS", "-1"), ("WA_FALLBACK_TEMPLATE", "Job Offer"),
    ("TELEGRAM_WEBHOOK_SECRET", "has spaces!"),
])
def test_bad_optional_values_are_named(name, value):
    (p,) = problems_for(**{name: value})
    assert p.startswith(name)


def test_no_secret_value_ever_appears_in_the_report():
    secrets = {"DATABASE_URL": "psql 'postgresql://neondb_owner:hunter2-pw@ep-x.neon.tech/neondb'",
               "WHATSAPP_TOKEN": "EAAB leaked token value", "TELEGRAM_BOT_TOKEN": "bad token leaked-value",
               "FOUNDER_USER_ID": "< 987654321 --stage"}
    env = {**GOOD, **secrets}
    text = report(check_environment(env)[0])
    for needle in ("hunter2", "neondb_owner", "leaked", "987654321", "ep-x.neon.tech"):
        assert needle not in text


def test_the_pooled_address_is_recommended_but_not_required():
    unpooled = "postgresql://u:p@ep-quiet-night.eu-central-1.aws.neon.tech/neondb?sslmode=require"
    problems, warnings = check_environment({**GOOD, "DATABASE_URL": unpooled})
    assert problems == [] and any("-pooler" in w for w in warnings)


def test_a_missing_webhook_secret_is_a_warning_not_a_stop():
    env = {k: v for k, v in GOOD.items() if k != "TELEGRAM_WEBHOOK_SECRET"}
    problems, warnings = check_environment(env)
    assert problems == [] and any("TELEGRAM_WEBHOOK_SECRET" in w for w in warnings)


def test_require_valid_environment_raises_one_readable_error():
    with pytest.raises(ConfigError) as err:
        require_valid_environment({**GOOD, "DATABASE_URL": "<psql 'x'>", "FOUNDER_USER_ID": "no"})
    text = str(err.value)
    assert text.startswith("PLUMBERFLOW CANNOT START")
    assert "DATABASE_URL" in text and "FOUNDER_USER_ID" in text
    assert "no database was contacted" in text.lower()


def test_the_example_in_the_message_is_not_anyones_real_id():
    (p,) = problems_for(FOUNDER_USER_ID="oops")
    assert "123456789" in p


# ───────────────────────── the Fly settings ─────────────────────────

FLY = tomllib.loads((Path(__file__).resolve().parent.parent / "fly.toml").read_text())


def test_a_new_version_must_pass_its_health_check_before_it_takes_over():
    assert FLY["deploy"]["strategy"] == "bluegreen"
    (check,) = FLY["http_service"]["checks"]
    assert check["path"] == "/health" and check["method"] == "GET"


def test_the_health_check_gives_the_app_time_to_start():
    (check,) = FLY["http_service"]["checks"]
    seconds = lambda s: int(s.rstrip("s"))
    assert seconds(check["grace_period"]) >= 20 and seconds(check["timeout"]) < seconds(check["interval"])
