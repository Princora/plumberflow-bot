"""Completion verification service.

Wiring surface (all synchronous; call from your FastAPI handlers / a scheduler):

    svc = VerificationService(store, messenger)

    svc.register_acceptance(...)              # when a plumber takes a lead
    svc.on_plumber_message(plumber_id, text)  # every inbound plumber text; True if handled
    svc.on_customer_button(job_id, button_id) # B1 button taps
    svc.on_customer_reply(phone, label, ...)  # NLU-classified free-text replies
    svc.run_sweeps()                          # every ~10 minutes
    svc.is_leads_paused(plumber_id)           # your lead router must check this
    svc.mark_invoice_paid(invoice_id)         # when money lands
    svc.admin_resolve(job_id, "charge" | "no_charge", note)   # Prince's call

Claude Haiku should classify inbound customer text into one of:
    sorted | not_sorted | never_came | off_price | other_off
and plumber text can be passed raw; parsing.py handles the deterministic cases.
Outbound text only ever comes from templates.py.
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
from typing import Callable
from zoneinfo import ZoneInfo

from . import templates as T
from .parsing import (
    is_wrong, parse_a9_reply, parse_amount_cents, parse_choice, parse_intent,
)
from .resolution import A7_AFTER_DAYS, Outcome, resolve
from .store import Messenger, OPEN_STATUSES, Store

SAST = ZoneInfo("Africa/Johannesburg")
FEE_CENTS = 22_000  # flat R220

A3_AFTER = timedelta(hours=24)
B2_AFTER = timedelta(hours=24)
B1_AFTER = timedelta(hours=2)
PATTERN_THRESHOLD = 3

# Send windows (SAST, [start, end) hours). Sweeps simply wait for the window.
PLUMBER_WINDOW = (7, 21)
CUSTOMER_WINDOW = (8, 20)
CUSTOMER_ASK_WINDOW = (10, 18)

SETTLEABLE = ("accepted", "awaiting_customer", "unresolved", "disputed")
UNCLAIMED_STATUSES = ("accepted", "awaiting_customer", "unresolved")

HOLD_UNRESOLVED = "unresolved_jobs"
HOLD_UNPAID = "unpaid_invoice"


def _settleable(job: dict) -> bool:
    """Open jobs, plus two cases that must stay re-checkable by the customer's answer:
    charged-but-not-yet-invoiced (customer may say he never came) and plumber 'no go'
    (customer may say the job did happen)."""
    s = job["status"]
    if s in SETTLEABLE:
        return True
    if s == "charged" and not job.get("invoice_id"):
        return True
    return s == "no_charge" and job["plumber_claim"] == "no_go"


def _in_window(now: datetime, window: tuple[int, int]) -> bool:
    h = now.astimezone(SAST).hour
    return window[0] <= h < window[1]


def _monday(d: date) -> date:
    return d - timedelta(days=d.weekday())


def _fmt_range(start: date, end: date) -> str:
    if start.month == end.month:
        return f"{start.day}–{end.day} {end.strftime('%b')}"
    return f"{start.day} {start.strftime('%b')} – {end.day} {end.strftime('%b')}"


class VerificationService:
    def __init__(
        self,
        store: Store,
        messenger: Messenger,
        *,
        clock: Callable[[], datetime] | None = None,
        fee_cents: int = FEE_CENTS,
        credit_link: str | None = None,       # None disables the job-4 credit transition
        credit_amount_cents: int = 66_000,    # R660 = 3 jobs
        credit_jobs: int = 3,
    ) -> None:
        self.store = store
        self.msg = messenger
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self.fee_cents = fee_cents
        self.credit_link = credit_link
        self.credit_amount_cents = credit_amount_cents
        self.credit_jobs = credit_jobs

    def now(self) -> datetime:
        return self._clock()

    # ───────────────────────── Acceptance ─────────────────────────

    def register_acceptance(
        self, *, lead_id: str, plumber_id: str, customer_phone: str,
        customer_name: str | None, suburb: str | None, keyword: str | None,
    ) -> dict:
        """Call when a plumber takes a lead. Idempotent on lead_id."""
        existing = self.store.get_job_by_lead(lead_id)
        if existing:
            return existing
        job = self.store.insert_job({
            "lead_id": lead_id, "plumber_id": plumber_id,
            "customer_phone": customer_phone, "customer_name": customer_name,
            "suburb": suburb, "job_keyword": keyword,
            "accepted_at": self.now(), "status": "accepted",
            "fee_cents": self.fee_cents, "flags": [],
            "plumber_claim": None, "customer_response": None,
        })
        self.store.log_event("accepted", job_id=job["id"], plumber_id=plumber_id)
        return job

    # ───────────────────────── Helpers ─────────────────────────

    def is_leads_paused(self, plumber_id: str) -> bool:
        return bool(self.store.get_holds(plumber_id))

    def _short(self, job: dict) -> str:
        return " ".join(x for x in (job.get("suburb"), job.get("job_keyword")) if x) or "job"

    def _job_day(self, job: dict) -> str:
        return job["accepted_at"].astimezone(SAST).strftime("%A")

    def _age_days(self, job: dict) -> float:
        return (self.now() - job["accepted_at"]).total_seconds() / 86400

    def _plumber(self, job: dict) -> dict:
        return self.store.get_plumber(job["plumber_id"]) or {"id": job["plumber_id"], "first_name": "there"}

    def _flag(self, job: dict, *new: str) -> dict:
        merged = sorted(set(job["flags"]) | set(new))
        if merged != sorted(job["flags"]):
            return self.store.update_job(job["id"], flags=merged)
        return job

    # ───────────────────────── Settlement ─────────────────────────

    def _settle(self, job_id: str) -> dict:
        """Apply the resolution matrix to a job and act on the outcome."""
        job = self.store.get_job(job_id)
        if not job or not _settleable(job):
            return job
        res = resolve(job["plumber_claim"], job["customer_response"], self._age_days(job))
        before = job["status"]
        new_flags = set(res.flags) - set(job["flags"])
        job = self._flag(job, *res.flags)
        now = self.now()

        if res.outcome is Outcome.HOLD:
            return job

        if res.outcome is Outcome.CHARGE:
            if before == "charged":
                return job
            if job["plumber_claim"] is None:
                self._send_a7_notice(job)
                job = self.store.get_job(job["id"])
            job = self.store.update_job(job["id"], status="charged", charged_at=now, resolved_at=now)
            self.store.log_event("charged", job_id=job["id"], plumber_id=job["plumber_id"])

        elif res.outcome is Outcome.NO_CHARGE:
            if before != "no_charge":
                job = self.store.update_job(job["id"], status="no_charge", resolved_at=now)
                self.store.log_event("no_charge", job_id=job["id"], plumber_id=job["plumber_id"])
            if "no_go_but_customer_confirms" in new_flags:
                prior = sum(
                    1 for j in self.store.list_jobs(plumber_id=job["plumber_id"])
                    if "no_go_but_customer_confirms" in j["flags"]
                )
                self.msg.notify_founder(
                    f"FRAUD SIGNAL: {self._plumber(job)['first_name']} said no go on "
                    f"{self._short(job)} but the customer says the job happened. "
                    f"Count for this plumber: {prior}. One is a conversation, two is removal."
                )

        elif res.outcome is Outcome.DISPUTED:
            job = self.store.update_job(job["id"], status="disputed", charged_at=None, resolved_at=None)
            if before != "disputed":
                self.store.log_event("disputed", job_id=job["id"], plumber_id=job["plumber_id"],
                                     payload={"flags": list(res.flags)})
                self.msg.notify_founder(
                    f"DISPUTE: {self._short(job)} · plumber {self._plumber(job)['first_name']} "
                    f"(claim: {job['plumber_claim']}) · customer {job['customer_phone']} "
                    f"(says: {job['customer_response']}). Flags: {', '.join(res.flags)}. "
                    f"Job {job['id']}. Reply within 2 hours."
                )

        elif res.outcome is Outcome.UNRESOLVED:
            if before != "unresolved":
                job = self.store.update_job(job["id"], status="unresolved", resolved_at=now)
                self.store.log_event("unresolved", job_id=job["id"], plumber_id=job["plumber_id"])

        self._release_if_clear(job["plumber_id"])
        return job

    def _send_a7_notice(self, job: dict) -> None:
        plumber = self._plumber(job)
        self.msg.send_plumber(plumber, T.a7_customer_confirmed(
            plumber["first_name"], job.get("customer_name") or "The customer",
            job.get("job_keyword") or "job", self._job_day(job), job["fee_cents"],
        ))
        self.store.update_job(job["id"], a7_sent_at=self.now())
        self.store.set_plumber_state(job["plumber_id"], "a7_reply", job_id=job["id"])
        self.store.log_event("a7_sent", job_id=job["id"], plumber_id=job["plumber_id"])

    def _release_if_clear(self, plumber_id: str) -> None:
        if HOLD_UNRESOLVED in self.store.get_holds(plumber_id):
            n = len(self.store.list_jobs(statuses=("unresolved",), plumber_id=plumber_id))
            if n < PATTERN_THRESHOLD:
                self.store.clear_hold(plumber_id, HOLD_UNRESOLVED)

    # ───────────────────────── Plumber inbound ─────────────────────────

    def on_plumber_message(self, plumber_id: str, text: str) -> bool:
        plumber = self.store.get_plumber(plumber_id)
        if not plumber:
            return False

        state = self.store.get_plumber_state(plumber_id)
        if state and self._continue_state(plumber, state, text):
            return True

        intent = parse_intent(text)
        if intent is None:
            return False

        jobs = [j for j in self.store.list_jobs(statuses=UNCLAIMED_STATUSES, plumber_id=plumber_id)
                if j["plumber_claim"] is None]
        if not jobs:
            self.msg.send_plumber(plumber, T.no_open_jobs())
            return True
        if len(jobs) == 1:
            self._begin(plumber, jobs[0], intent, amount=parse_amount_cents(text) if intent == "done" else None)
            return True

        jobs = jobs[-6:]
        self.store.set_plumber_state(
            plumber_id, f"choose_{intent}", options=[j["id"] for j in jobs]
        )
        self.msg.send_plumber(plumber, T.which_one(
            [(i + 1, self._short(j), self._job_day(j)) for i, j in enumerate(jobs)]
        ))
        return True

    def _begin(self, plumber: dict, job: dict, intent: str, amount: int | None = None) -> None:
        if intent == "done":
            if amount:
                self._record_done(plumber, job, amount)
            else:
                self.store.set_plumber_state(plumber["id"], "awaiting_value", job_id=job["id"])
                self.msg.send_plumber(plumber, T.a4_ask_value())
        else:
            self._record_no_go(plumber, job)

    def _record_done(self, plumber: dict, job: dict, cents: int) -> None:
        self.store.clear_plumber_state(plumber["id"])
        job = self.store.update_job(
            job["id"], plumber_claim="done", plumber_claim_at=self.now(),
            job_value_cents=cents, status="awaiting_customer",
        )
        self.store.log_event("claimed_done", job_id=job["id"], plumber_id=plumber["id"],
                             payload={"value_cents": cents})
        self.msg.send_plumber(plumber, T.a5_value_logged(cents, job["fee_cents"]))
        self._settle(job["id"])

    def _record_no_go(self, plumber: dict, job: dict, ask_reason: bool = True) -> None:
        job = self.store.update_job(job["id"], plumber_claim="no_go", plumber_claim_at=self.now())
        self.store.log_event("claimed_no_go", job_id=job["id"], plumber_id=plumber["id"])
        if ask_reason:
            self.store.set_plumber_state(plumber["id"], "awaiting_no_go_reason", job_id=job["id"])
            self.msg.send_plumber(plumber, T.a6_no_go_ack())
        self._settle(job["id"])

    def _continue_state(self, plumber: dict, state: dict, text: str) -> bool:
        pid, step = plumber["id"], state["step"]

        if step != "a9" and parse_intent(text) is not None:
            self.store.clear_plumber_state(pid)   # a fresh command overrides a stale prompt
            return False

        if step in ("choose_done", "choose_no_go"):
            n = parse_choice(text)
            options = state["options"] or []
            if n is None or not 1 <= n <= len(options):
                return False
            self.store.clear_plumber_state(pid)
            job = self.store.get_job(options[n - 1])
            self._begin(plumber, job, "done" if step == "choose_done" else "no_go")
            return True

        if step == "awaiting_value":
            cents = parse_amount_cents(text)
            if cents is None:
                tries = state.get("tries", 0) + 1
                if tries >= 2:
                    self.store.clear_plumber_state(pid)
                    return False
                self.store.set_plumber_state(pid, "awaiting_value", job_id=state["job_id"], tries=tries)
                self.msg.send_plumber(plumber, T.a4_value_retry())
                return True
            self._record_done(plumber, self.store.get_job(state["job_id"]), cents)
            return True

        if step == "awaiting_no_go_reason":
            self.store.update_job(state["job_id"], no_go_reason=text.strip()[:300])
            self.store.clear_plumber_state(pid)
            self.msg.send_plumber(plumber, T.a6_reason_ack())
            return True

        if step == "a7_reply":
            self.store.clear_plumber_state(pid)
            if not is_wrong(text):
                return False
            job = self.store.get_job(state["job_id"])
            self.store.update_job(job["id"], status="disputed", charged_at=None, resolved_at=None)
            self._flag(self.store.get_job(job["id"]), "charge_contested_by_plumber")
            self.store.log_event("a7_contested", job_id=job["id"], plumber_id=pid)
            self.msg.send_plumber(plumber, T.a7_pulled())
            self.msg.notify_founder(
                f"CONTESTED CHARGE: {plumber['first_name']} says {self._short(job)} is wrong "
                f"after the customer confirmed it. Job {job['id']}."
            )
            return True

        if step == "a9":
            return self._handle_a9_reply(plumber, state, text)

        return False

    def _handle_a9_reply(self, plumber: dict, state: dict, text: str) -> bool:
        items = parse_a9_reply(text)
        if not items:
            return False
        options = state["options"] or []
        logged, needs_value = 0, None
        for n, claim, cents in items:
            if not 1 <= n <= len(options):
                continue
            job = self.store.get_job(options[n - 1])
            if job["plumber_claim"] is not None:
                continue
            if claim == "no_go":
                self._record_no_go(plumber, job, ask_reason=False)
                logged += 1
            elif cents:
                self._record_done(plumber, job, cents)
                logged += 1
            elif needs_value is None:
                needs_value = job
        remaining = len(self.store.list_jobs(statuses=("unresolved",), plumber_id=plumber["id"]))
        if needs_value is not None:
            self.store.set_plumber_state(plumber["id"], "awaiting_value", job_id=needs_value["id"])
            self.msg.send_plumber(plumber, T.a4_ask_value())
            return True
        self.store.clear_plumber_state(plumber["id"])
        self.msg.send_plumber(plumber, T.a9_progress(logged, remaining))
        return True

    # ───────────────────────── Customer inbound ─────────────────────────

    def on_customer_button(self, job_id: str, button_id: str) -> None:
        job = self.store.get_job(job_id)
        if not job:
            return
        name = job.get("customer_name") or "there"
        now = self.now()
        if button_id == "yes":
            self.store.update_job(job_id, customer_response="yes", customer_response_at=now)
            self.msg.send_customer(job["customer_phone"], T.b3_thanks(name))
        elif button_id == "off":
            self.store.update_job(job_id, customer_response="off", customer_response_at=now)
            self.msg.send_customer(job["customer_phone"], T.b4_whats_off())
        else:
            return
        self.store.log_event(f"customer_{button_id}", job_id=job_id, plumber_id=job["plumber_id"])
        self._settle(job_id)

    def on_customer_reply(
        self, phone: str, label: str, *, actual_value_cents: int | None = None
    ) -> bool:
        """label from NLU: sorted | not_sorted | never_came | off_price | other_off."""
        job = self._job_awaiting_customer(phone)
        if not job:
            return False
        name = job.get("customer_name") or "there"
        now = self.now()
        plumber_first = self._plumber(job)["first_name"]

        if label == "sorted":
            self.store.update_job(job["id"], customer_response="yes", customer_response_at=now)
            self.msg.send_customer(phone, T.b3_thanks(name))
        elif label == "not_sorted":
            self.store.update_job(job["id"], customer_response="not_sorted", customer_response_at=now)
            self.msg.send_customer(phone, T.customer_ack())
        elif label == "never_came":
            self.store.update_job(job["id"], customer_response="never_came", customer_response_at=now)
            self.msg.send_customer(phone, T.b5_never_came())
            self.msg.notify_founder(
                f"HIGH SIGNAL: customer {phone} says {plumber_first} never came for "
                f"{self._short(job)}. Plumber claim: {job['plumber_claim']}. Job {job['id']}."
            )
        elif label == "off_price":
            self.store.update_job(job["id"], customer_response="off_price", customer_response_at=now)
            self.msg.send_customer(phone, T.b6_price_flagged(
                plumber_first, job.get("job_value_cents") or 0, actual_value_cents))
            self.msg.notify_founder(
                f"PRICE FLAG: {plumber_first} logged {job.get('job_value_cents')} cents, customer "
                f"says {actual_value_cents}. Job {job['id']}."
            )
        elif label == "other_off":
            self.store.update_job(job["id"], customer_response="off", customer_response_at=now)
            self.msg.notify_founder(f"CUSTOMER SAYS SOMETHING'S OFF: {phone}, {self._short(job)}. Job {job['id']}.")
        else:
            return False
        self.store.log_event(f"customer_{label}", job_id=job["id"], plumber_id=job["plumber_id"])
        self._settle(job["id"])
        return True

    def _job_awaiting_customer(self, phone: str) -> dict | None:
        jobs = self.store.list_jobs(
            statuses=("accepted", "awaiting_customer", "disputed", "unresolved", "charged", "no_charge"),
            customer_phone=phone,
        )
        candidates = [
            j for j in jobs
            if _settleable(j) and j["customer_response"] in (None, "off", "not_sorted")
            and (j["b1_sent_at"] or j["b2_sent_at"])
        ]
        return candidates[-1] if candidates else None

    # ───────────────────────── Sweeps ─────────────────────────

    def run_sweeps(self) -> None:
        self.sweep_jobs()
        self.sweep_patterns()
        self.sweep_invoices()

    def sweep_jobs(self) -> None:
        now = self.now()
        candidates = [
            j for j in self.store.list_jobs(statuses=OPEN_STATUSES + ("charged", "no_charge"))
            if j["status"] in OPEN_STATUSES
            or (j["status"] == "charged" and not j.get("invoice_id"))
            or (j["status"] == "no_charge" and j["plumber_claim"] == "no_go"
                and now - j["accepted_at"] < timedelta(days=7))
        ]
        for job in candidates:
            age = now - job["accepted_at"]
            plumber = self._plumber(job)
            name = job.get("customer_name") or "there"

            if job["plumber_claim"] is None and job["status"] in OPEN_STATUSES:
                if age >= A3_AFTER and not job["a3_sent_at"] and _in_window(now, PLUMBER_WINDOW):
                    self.msg.send_plumber(plumber, T.a3_job_close_prompt(
                        plumber["first_name"], job.get("suburb") or "", job.get("job_keyword") or "job"))
                    job = self.store.update_job(job["id"], a3_sent_at=now)

            if job["plumber_claim"] in (None, "no_go") and job["status"] != "charged":
                if age >= B2_AFTER and not job["b2_sent_at"] and _in_window(now, CUSTOMER_ASK_WINDOW):
                    self.msg.send_customer(job["customer_phone"], T.b2_did_it_get_sorted(
                        name, plumber["first_name"], job.get("job_keyword") or "job"))
                    job = self.store.update_job(job["id"], b2_sent_at=now)

            if (
                job["plumber_claim"] == "done" and not job["b1_sent_at"]
                and job["customer_response"] is None
                and now - job["plumber_claim_at"] >= B1_AFTER
                and _in_window(now, CUSTOMER_WINDOW)
            ):
                self.msg.send_customer_buttons(
                    job["customer_phone"],
                    T.b1_confirm_logged(name, plumber["first_name"],
                                        job.get("job_keyword") or "job", job["job_value_cents"]),
                    T.B1_BUTTONS, job["id"],
                )
                job = self.store.update_job(job["id"], b1_sent_at=now)

            self._settle(job["id"])

    def sweep_patterns(self) -> None:
        by_plumber: dict[str, list[dict]] = {}
        for j in self.store.list_jobs(statuses=("unresolved",)):
            by_plumber.setdefault(j["plumber_id"], []).append(j)
        for pid, jobs in by_plumber.items():
            if len(jobs) < PATTERN_THRESHOLD or HOLD_UNRESOLVED in self.store.get_holds(pid):
                continue
            if not _in_window(self.now(), PLUMBER_WINDOW):
                continue
            plumber = self.store.get_plumber(pid)
            if not plumber:
                continue
            jobs = jobs[-6:]
            self.store.set_plumber_state(pid, "a9", options=[j["id"] for j in jobs])
            self.store.add_hold(pid, HOLD_UNRESOLVED)
            self.store.log_event("pattern_notice", plumber_id=pid, payload={"n": len(jobs)})
            self.msg.send_plumber(plumber, T.a9_pattern(
                plumber["first_name"], [(i + 1, self._short(j), self._job_day(j)) for i, j in enumerate(jobs)]))

    # ───────────────────────── Invoicing ─────────────────────────

    def sweep_invoices(self) -> None:
        self._send_weekly_invoices()
        self._chase_overdue()

    def _send_weekly_invoices(self) -> None:
        now = self.now()
        local = now.astimezone(SAST)
        # Friday 17:00 SAST onward, through the weekend, until Monday.
        if not (local.weekday() == 4 and local.hour >= 17 or local.weekday() in (5, 6)):
            return
        week_start = _monday(local.date())
        week_end = week_start + timedelta(days=4)
        due = datetime.combine(week_start + timedelta(days=7), time(23, 59), SAST)
        rng = _fmt_range(week_start, week_end)

        for plumber in self.store.list_active_plumbers():
            if any(i["week_start"] == week_start for i in self.store.list_invoices(plumber_id=plumber["id"])):
                continue
            jobs = [j for j in self.store.list_jobs(statuses=("charged",), plumber_id=plumber["id"])
                    if not j.get("invoice_id")]
            if not jobs:
                inv = self.store.insert_invoice({
                    "plumber_id": plumber["id"], "week_start": week_start, "job_count": 0,
                    "total_cents": 0, "status": "zero", "sent_at": now,
                })
                self.msg.send_plumber(plumber, T.a10_zero_week(plumber["first_name"], rng, None, None))
                continue
            total = sum(j["fee_cents"] for j in jobs)
            inv = self.store.insert_invoice({
                "plumber_id": plumber["id"], "week_start": week_start, "job_count": len(jobs),
                "total_cents": total, "status": "sent", "sent_at": now, "due_at": due,
            })
            for j in jobs:
                self.store.update_job(j["id"], invoice_id=inv["id"])
            lines = [f"{self._short(j)} · {self._job_day(j)} · {T.rand(j['fee_cents'])}" for j in jobs]
            self.msg.send_plumber(plumber, T.a10_invoice(plumber["first_name"], rng, lines, total))
            self.store.log_event("invoice_sent", plumber_id=plumber["id"],
                                 payload={"invoice_id": inv["id"], "total_cents": total})

    def _chase_overdue(self) -> None:
        now = self.now()
        for inv in self.store.list_invoices(statuses=("sent",)):
            if not inv.get("due_at"):
                continue
            due_day = inv["due_at"].astimezone(SAST).date()
            remind_at = datetime.combine(due_day + timedelta(days=1), time(9, 0), SAST)
            pause_at = datetime.combine(due_day + timedelta(days=3), time(9, 0), SAST)
            plumber = self.store.get_plumber(inv["plumber_id"])
            if not plumber:
                continue
            if not inv.get("reminded_at") and now >= remind_at:
                self.store.update_invoice(inv["id"], reminded_at=now)
                self.msg.send_plumber(plumber, T.a11_overdue(plumber["first_name"], inv["total_cents"]))
            elif (
                inv.get("reminded_at") and not inv.get("paused_at") and now >= pause_at
                and now - inv["reminded_at"] >= timedelta(hours=24)   # always a reminder first
            ):
                self.store.add_hold(plumber["id"], HOLD_UNPAID)
                self.store.update_invoice(inv["id"], paused_at=now)
                self.msg.send_plumber(plumber, T.a12_paused(plumber["first_name"], inv["total_cents"]))
                self.store.log_event("leads_paused", plumber_id=plumber["id"],
                                     payload={"invoice_id": inv["id"]})

    def mark_invoice_paid(self, invoice_id: str) -> None:
        inv = self.store.get_invoice(invoice_id)
        if not inv or inv["status"] == "paid":
            return
        self.store.update_invoice(invoice_id, status="paid", paid_at=self.now())
        plumber = self.store.get_plumber(inv["plumber_id"])
        restored = False
        if HOLD_UNPAID in self.store.get_holds(inv["plumber_id"]):
            if not self.store.list_invoices(plumber_id=inv["plumber_id"], statuses=("sent",)):
                self.store.clear_hold(inv["plumber_id"], HOLD_UNPAID)
                restored = True
        if plumber:
            self.msg.send_plumber(plumber, T.payment_received(inv["total_cents"], restored=restored))
        self.store.log_event("invoice_paid", plumber_id=inv["plumber_id"],
                             payload={"invoice_id": invoice_id})
        self._maybe_credit_transition(inv["plumber_id"])

    def _maybe_credit_transition(self, plumber_id: str) -> None:
        if not self.credit_link or self.store.has_event(plumber_id, "credit_transition_sent"):
            return
        settled = sum(
            i["job_count"] for i in self.store.list_invoices(plumber_id=plumber_id, statuses=("paid",))
        )
        if settled < 3:
            return
        plumber = self.store.get_plumber(plumber_id)
        if not plumber:
            return
        self.msg.send_plumber(plumber, T.credit_transition(
            plumber["first_name"], self.credit_amount_cents, self.credit_jobs,
            self.fee_cents, self.credit_link))
        self.store.log_event("credit_transition_sent", plumber_id=plumber_id)

    # ───────────────────────── Prince's override ─────────────────────────

    def admin_resolve(self, job_id: str, outcome: str, note: str = "") -> dict:
        """outcome: 'charge' | 'no_charge'. For disputed/unresolved jobs only."""
        job = self.store.get_job(job_id)
        if not job or job["status"] not in ("disputed", "unresolved"):
            raise ValueError("job is not awaiting a human decision")
        now = self.now()
        if outcome == "charge":
            job = self.store.update_job(job_id, status="charged", charged_at=now, resolved_at=now)
        elif outcome == "no_charge":
            job = self.store.update_job(job_id, status="no_charge", resolved_at=now)
        else:
            raise ValueError("outcome must be 'charge' or 'no_charge'")
        self.store.log_event("admin_resolved", job_id=job_id, plumber_id=job["plumber_id"],
                             payload={"outcome": outcome, "note": note})
        self._release_if_clear(job["plumber_id"])
        return job
