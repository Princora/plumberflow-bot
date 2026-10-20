"""Checks the Fly secrets before the bot starts, and says plainly which one is wrong.

A first deploy failed because two secrets had extra text in them: the Telegram id was
saved as "< 403350361 --stage" and the database setting began with the pasted psql command.
The bot only said "invalid literal for int()", which names neither. This checks every
setting up front and reports all the problems at once, naming each setting and what is
wrong with it.

Values are NEVER printed: a database URL or an API key in a log is a leak. Problems are
described ("has spaces", "starts with psql") instead.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from urllib.parse import urlsplit


class ConfigError(RuntimeError):
    """The bot cannot start because of how it is configured. Never carries a secret value."""


def _stray_text(value: str) -> list[str]:
    """Signs that something was pasted along with the value (a command, quotes, brackets)."""
    found = []
    if value != value.strip():
        found.append("spaces or a new line at the start or end")
    if re.search(r"\s", value.strip()):
        found.append("spaces inside it")
    if value[:1] in "\"'`<>" or value[-1:] in "\"'`<>":
        found.append("a quote or a < > at the start or end")
    if re.search(r"(?:^|\s)--?[A-Za-z]", value):
        found.append("what looks like a command option (such as --stage)")
    return found


def _describe(parts: list[str]) -> str:
    return "; it has " + ", ".join(parts) if parts else ""


def _digits(what: str, example: str):
    def check(value: str) -> str | None:
        if re.fullmatch(r"\d+", value):
            return None
        return f"must be digits only, {what}, for example {example}{_describe(_stray_text(value))}"
    return check


def _database_url(value: str) -> str | None:
    guide = ("It must be only the connection string, starting with postgresql://. In the Neon "
             "console open Connection details, turn on Pooled connection, and copy the connection "
             "string, not the psql command")
    if re.match(r"\s*<?\s*psql\b", value, re.I):
        return "looks like the whole psql command was pasted. " + guide
    if not re.match(r"postgres(ql)?://", value):
        return f"does not start with postgresql://{_describe(_stray_text(value))}. " + guide
    parts = urlsplit(value)
    if _stray_text(value):
        return f"has extra text in it{_describe(_stray_text(value))}. " + guide
    if not parts.hostname or not parts.username:
        return "is missing the host or the user name. " + guide
    return None


def _telegram_token(value: str) -> str | None:
    if re.fullmatch(r"\d{5,}:[A-Za-z0-9_-]{20,}", value):
        return None
    return "does not look like a Telegram bot token (numbers, a colon, then letters and numbers)" + _describe(_stray_text(value))


def _plain_secret(value: str) -> str | None:
    stray = _stray_text(value)
    return f"has extra text in it{_describe(stray)}" if stray else None


def _chat_id(value: str) -> str | None:
    if re.fullmatch(r"-?\d+", value):
        return None
    return "must be a number (a group chat id starts with -)" + _describe(_stray_text(value))


def _phone(value: str) -> str | None:
    if re.fullmatch(r"\+?[\d ]{9,18}", value):
        return None
    return "must be a phone number with the country code, for example 27821234567" + _describe(_stray_text(value))


def _template_name(value: str) -> str | None:
    if re.fullmatch(r"[a-z0-9_]+", value):
        return None
    return "must be the template name exactly as Meta shows it (lowercase letters, numbers and _)"


def _webhook_secret(value: str) -> str | None:
    if re.fullmatch(r"[A-Za-z0-9_-]{1,256}", value):
        return None
    return "may only contain letters, numbers, _ and - (that is Telegram's rule)" + _describe(_stray_text(value))


# (names tried in order, check, required)
_SETTINGS = [
    (("DATABASE_URL",), _database_url, True),
    (("WA_TOKEN", "WHATSAPP_TOKEN"), _plain_secret, True),
    (("WA_PHONE_NUMBER_ID", "PHONE_NUMBER_ID"), _digits("the WhatsApp phone number id from Meta", "109876543210987"), True),
    (("TELEGRAM_TOKEN", "TELEGRAM_BOT_TOKEN"), _telegram_token, True),
    (("FOUNDER_CHAT_ID", "TELEGRAM_CHAT_ID"), _chat_id, True),
    (("FOUNDER_USER_ID",), _digits("your Telegram user id", "123456789"), True),
    (("ANTHROPIC_API_KEY",), _plain_secret, False),
    (("ADMIN_PHONE",), _phone, False),
    (("TELEGRAM_WEBHOOK_SECRET",), _webhook_secret, False),
    (("VERIFY_TOKEN",), _plain_secret, False),
    (("WA_APP_SECRET", "META_APP_SECRET"), _plain_secret, False),
    (("WA_FALLBACK_TEMPLATE",), _template_name, False),
    (("TRIAL_LEADS",), _digits("how many free jobs a new plumber gets", "2"), False),
]


def check_environment(environ: Mapping[str, str]) -> tuple[list[str], list[str]]:
    """(problems that stop the bot, warnings). Each is one plain sentence. Never contains a value."""
    problems: list[str] = []
    warnings: list[str] = []
    for names, check, required in _SETTINGS:
        name = next((n for n in names if environ.get(n)), None)
        label = " or ".join(names)
        if name is None:
            if required:
                problems.append(f"{label} is not set.")
            continue
        issue = check(environ[name])
        if issue:
            problems.append(f"{name} {issue}.")
    url = environ.get("DATABASE_URL", "")
    if url and not _database_url(url) and "-pooler" not in (urlsplit(url).hostname or ""):
        warnings.append("DATABASE_URL is not the pooled Neon address (the host has no -pooler). "
                        "It works, but the pooled one is what this bot is built for.")
    if not environ.get("TELEGRAM_WEBHOOK_SECRET"):
        warnings.append("TELEGRAM_WEBHOOK_SECRET is not set, so /telegram accepts any caller.")
    return problems, warnings


def report(problems: list[str]) -> str:
    lines = ["PLUMBERFLOW CANNOT START. Fix these settings, then deploy again:", ""]
    lines += [f"  - {p}" for p in problems]
    lines += ["", "Set one with:  fly secrets set -a plumberflow NAME=value",
              "Use the value only: no < >, no quotes, no psql, no --stage inside the value.",
              "Nothing was changed and no database was contacted."]
    return "\n".join(lines)


def require_valid_environment(environ: Mapping[str, str]) -> list[str]:
    """Raises ConfigError listing every problem; returns the warnings when all is well."""
    problems, warnings = check_environment(environ)
    if problems:
        raise ConfigError(report(problems))
    return warnings
