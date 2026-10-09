"""Pure resolution logic: what happens to a job given both sides' claims.

No I/O, no clock, no messaging. This is the matrix from the completion
verification module, in code, so it can be tested exhaustively.

Rules that carry the weight:
  * Never charge on customer silence alone. A charge needs a plumber claim
    ("done") or a customer confirmation plus the day-4 notice to the plumber.
  * Silence from both sides is a write-off (UNRESOLVED), never a charge.
  * "no go" + customer says the job happened is the theft signal: no charge,
    flagged hard.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

A7_AFTER_DAYS = 4          # plumber silent, customer confirmed -> notice + charge
UNRESOLVED_AFTER_DAYS = 5  # both silent -> write off


class Outcome(str, Enum):
    CHARGE = "charge"
    NO_CHARGE = "no_charge"
    HOLD = "hold"              # not enough information yet; check again later
    DISPUTED = "disputed"      # needs a human (Prince)
    UNRESOLVED = "unresolved"  # both sides silent past the write-off point


@dataclass(frozen=True)
class Resolution:
    outcome: Outcome
    flags: tuple[str, ...] = ()


# Customer responses that mean "the job did happen".
JOB_HAPPENED = frozenset({"yes", "off_price"})


def resolve(claim: str | None, response: str | None, age_days: float) -> Resolution:
    """claim: 'done' | 'no_go' | None.  response: customer response | None."""
    if claim == "done":
        if response == "never_came":
            return Resolution(Outcome.DISPUTED, ("done_but_never_came",))
        if response == "off":
            return Resolution(Outcome.DISPUTED, ("customer_off_unclassified",))
        if response == "off_price":
            return Resolution(Outcome.CHARGE, ("price_mismatch",))
        # yes / silence / not_sorted: the plumber's claim stands unopposed.
        return Resolution(Outcome.CHARGE)

    if claim == "no_go":
        if response in JOB_HAPPENED:
            return Resolution(Outcome.NO_CHARGE, ("no_go_but_customer_confirms",))
        return Resolution(Outcome.NO_CHARGE)

    # No plumber claim.
    if response in JOB_HAPPENED:
        return Resolution(Outcome.CHARGE if age_days >= A7_AFTER_DAYS else Outcome.HOLD)
    if response == "never_came":
        return Resolution(Outcome.NO_CHARGE)
    if response == "off":
        return Resolution(Outcome.DISPUTED, ("customer_off_unclassified",))
    # Silence or "not_sorted".
    if age_days >= UNRESOLVED_AFTER_DAYS:
        return Resolution(Outcome.UNRESOLVED)
    return Resolution(Outcome.HOLD)
