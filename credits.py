"""Lead credits: the packs a plumber can buy once the free trial is over.

One credit is one lead taken. Each credit is R220; the 5 pack is 5% off and the 10 pack 10% off.
Prices are whole rand, so the discounts come out exact: R440, R1,045 and R1,980.
"""

from __future__ import annotations

import re

from verification.templates import rand

CREDIT_PRICE_CENTS = 22_000

# credits in the pack -> percent off the list price
PACK_DISCOUNT_PERCENT = {2: 0, 5: 5, 10: 10}
PACK_SIZES = tuple(sorted(PACK_DISCOUNT_PERCENT))


def pack_price_cents(credits: int) -> int:
    if credits not in PACK_DISCOUNT_PERCENT:
        raise ValueError(f"no such pack: {credits}")
    return credits * CREDIT_PRICE_CENTS * (100 - PACK_DISCOUNT_PERCENT[credits]) // 100


def pack_line(credits: int) -> str:
    off = PACK_DISCOUNT_PERCENT[credits]
    return f"{credits} credits · {rand(pack_price_cents(credits))}" + (f" ({off}% off)" if off else "")


_PACK_REPLY = re.compile(r"^\s*(?:buy\s+)?(\d{1,2})(?:\s*credits?)?\s*$", re.I)


def parse_pack(text: str) -> int | None:
    """"5", "5 credits", "buy 10" -> the pack size. Anything else, or a size we don't sell, is None."""
    m = _PACK_REPLY.match(text or "")
    n = int(m.group(1)) if m else None
    return n if n in PACK_DISCOUNT_PERCENT else None


def local_number(raw: str) -> str:
    """A South African cellphone number the way people dial it: +27 82 123 4567 or 27821234567
    become 0821234567. Anything that isn't a plain SA number is returned as typed, minus spaces."""
    text = re.sub(r"[\s\-()]", "", raw or "")
    digits = text[1:] if text.startswith("+") else text
    if digits.startswith("0027"):
        digits = digits[2:]
    if digits.isdigit() and digits.startswith("27") and len(digits) == 11:
        return "0" + digits[2:]
    return text
