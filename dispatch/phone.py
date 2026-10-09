"""Phone normalisation. Meta sends 27796051126; people type 079 605 1126 or +27 79 605 1126."""
import re


def normalize_phone(raw: str | None) -> str:
    digits = re.sub(r"\D", "", raw or "")
    if digits.startswith("00"):
        digits = digits[2:]
    if digits.startswith("0") and len(digits) == 10:     # South African national format
        digits = "27" + digits[1:]
    return digits
