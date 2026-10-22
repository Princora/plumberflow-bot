"""Builds the dispatch and verification stack once, from environment variables.

The old variable names (WHATSAPP_TOKEN, PHONE_NUMBER_ID, TELEGRAM_BOT_TOKEN,
TELEGRAM_CHAT_ID) are still read, so secrets already set on Fly keep working.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass

from app_store import AppStore
from dispatch import DispatchService
from dispatch.telegram import TelegramClient
from dispatch.whatsapp import CloudMessenger, WhatsAppCloud
from config_check import require_valid_environment
from verification import VerificationService

logger = logging.getLogger(__name__)


def env(*names: str, default: str | None = None) -> str | None:
    for name in names:
        value = os.environ.get(name)
        if value:
            return value
    return default


def require(*names: str) -> str:
    value = env(*names)
    if not value:
        raise RuntimeError(f"Missing environment variable: {' or '.join(names)}")
    return value


@dataclass
class Services:
    store: AppStore
    wa: WhatsAppCloud
    tg: TelegramClient
    messenger: CloudMessenger
    ver: VerificationService
    disp: DispatchService
    founder_user_id: int


def _build_interpreter():
    """Haiku reads the replies the strict parsers cannot. Without a key, strict parsing only."""
    key = env("ANTHROPIC_API_KEY")
    if not key:
        return None
    from anthropic import Anthropic
    from verification.interpret import ClaudeReplyInterpreter
    return ClaudeReplyInterpreter(Anthropic(api_key=key, timeout=8.0, max_retries=1))


def build_services() -> Services:
    # Check every setting before touching anything, so one bad secret is reported by name
    # (all of them at once) instead of a cryptic error from deep inside startup.
    for warning in require_valid_environment(os.environ):
        logger.warning(warning)
    store = AppStore(require("DATABASE_URL"))
    wa = WhatsAppCloud(require("WA_TOKEN", "WHATSAPP_TOKEN"),
                       require("WA_PHONE_NUMBER_ID", "PHONE_NUMBER_ID"))
    tg = TelegramClient(require("TELEGRAM_TOKEN", "TELEGRAM_BOT_TOKEN"),
                        require("FOUNDER_CHAT_ID", "TELEGRAM_CHAT_ID"))
    messenger = CloudMessenger(wa, tg, fallback_template=env("WA_FALLBACK_TEMPLATE"),
                               template_lang=env("WA_TEMPLATE_LANG") or "en")
    ver = VerificationService(store, messenger, interpreter=_build_interpreter())
    disp = DispatchService(store, ver, messenger, tg)
    return Services(store, wa, tg, messenger, ver, disp, int(require("FOUNDER_USER_ID")))
