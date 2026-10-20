"""Storage and messaging interfaces, plus an in-memory store for tests.

The service only talks to `Store` and `Messenger`. PostgresStore (postgres_store.py)
implements Store against the tables in migrations/001_job_lifecycle.sql; the
in-memory store here is what most tests exercise.
"""

from __future__ import annotations

import copy
import uuid
from typing import Any, Protocol

OPEN_STATUSES = ("accepted", "awaiting_customer")


class Messenger(Protocol):
    def send_plumber(self, plumber: dict, text: str) -> None: ...
    def send_customer(self, phone: str, text: str) -> None: ...
    def send_customer_buttons(
        self, phone: str, text: str, buttons: list[tuple[str, str]], job_id: str
    ) -> None:
        """Button ids come back to the service as on_customer_button(job_id, id)."""
    def notify_founder(self, text: str) -> None: ...


class Store(Protocol):
    # jobs
    def insert_job(self, row: dict) -> dict: ...
    def get_job(self, job_id: str) -> dict | None: ...
    def get_job_by_lead(self, lead_id: str) -> dict | None: ...
    def update_job(self, job_id: str, **fields: Any) -> dict: ...
    def list_jobs(
        self, *, statuses: tuple[str, ...] | None = None, plumber_id: str | None = None,
        customer_phone: str | None = None,
    ) -> list[dict]: ...
    # plumbers
    def get_plumber(self, plumber_id: str) -> dict | None: ...
    def list_active_plumbers(self) -> list[dict]: ...
    def use_trial_lead(self, plumber_id: str) -> int | None: ...
    def grant_trial_leads(self, plumber_id: str, n: int) -> int: ...
    # conversation state
    def get_plumber_state(self, plumber_id: str) -> dict | None: ...
    def set_plumber_state(
        self, plumber_id: str, step: str, job_id: str | None = None,
        options: list[str] | None = None, tries: int = 0,
    ) -> None: ...
    def clear_plumber_state(self, plumber_id: str) -> None: ...
    # holds
    def add_hold(self, plumber_id: str, reason: str) -> None: ...
    def clear_hold(self, plumber_id: str, reason: str) -> None: ...
    def get_holds(self, plumber_id: str) -> set[str]: ...
    # invoices
    def insert_invoice(self, row: dict) -> dict: ...
    def get_invoice(self, invoice_id: str) -> dict | None: ...
    def update_invoice(self, invoice_id: str, **fields: Any) -> dict: ...
    def list_invoices(
        self, *, plumber_id: str | None = None, statuses: tuple[str, ...] | None = None
    ) -> list[dict]: ...
    # audit
    def log_event(
        self, kind: str, *, job_id: str | None = None, plumber_id: str | None = None,
        payload: dict | None = None,
    ) -> None: ...
    def has_event(self, plumber_id: str, kind: str) -> bool: ...


# ───────────────────────── In-memory (tests, local dev) ─────────────────────────

class InMemoryStore:
    def __init__(self) -> None:
        self.jobs: dict[str, dict] = {}
        self.plumbers: dict[str, dict] = {}
        self.states: dict[str, dict] = {}
        self.holds: set[tuple[str, str]] = set()
        self.invoices: dict[str, dict] = {}
        self.events: list[dict] = []

    def add_plumber(self, first_name: str, *, active: bool = True, **extra: Any) -> dict:
        p = {"id": str(uuid.uuid4()), "first_name": first_name, "active": active, **extra}
        self.plumbers[p["id"]] = p
        return p

    # jobs
    JOB_DEFAULTS = {
        "plumber_claim": None, "plumber_claim_at": None, "job_value_cents": None,
        "no_go_reason": None, "customer_response": None, "customer_response_at": None,
        "status": "accepted", "flags": [], "charged_at": None, "resolved_at": None,
        "invoice_id": None, "a3_sent_at": None, "b1_sent_at": None, "b2_sent_at": None,
        "a7_sent_at": None,
    }

    def insert_job(self, row):
        row = copy.deepcopy(row)
        row.setdefault("id", str(uuid.uuid4()))
        for k, v in self.JOB_DEFAULTS.items():     # mirror the column defaults in the migration
            row.setdefault(k, copy.deepcopy(v))
        self.jobs[row["id"]] = row
        return copy.deepcopy(row)

    def get_job(self, job_id):
        j = self.jobs.get(job_id)
        return copy.deepcopy(j) if j else None

    def get_job_by_lead(self, lead_id):
        for j in self.jobs.values():
            if j["lead_id"] == lead_id:
                return copy.deepcopy(j)
        return None

    def update_job(self, job_id, **fields):
        self.jobs[job_id].update(fields)
        return copy.deepcopy(self.jobs[job_id])

    def list_jobs(self, *, statuses=None, plumber_id=None, customer_phone=None):
        out = []
        for j in self.jobs.values():
            if statuses is not None and j["status"] not in statuses:
                continue
            if plumber_id is not None and j["plumber_id"] != plumber_id:
                continue
            if customer_phone is not None and j["customer_phone"] != customer_phone:
                continue
            out.append(copy.deepcopy(j))
        return sorted(out, key=lambda j: j["accepted_at"])

    # plumbers
    def get_plumber(self, plumber_id):
        return copy.deepcopy(self.plumbers.get(plumber_id))

    def list_active_plumbers(self):
        return [copy.deepcopy(p) for p in self.plumbers.values() if p.get("active")]

    def use_trial_lead(self, plumber_id):
        p = self.plumbers.get(plumber_id)
        if not p or (p.get("trial_stage") or 0) <= 0:
            return None
        p["trial_stage"] -= 1
        return p["trial_stage"]

    def grant_trial_leads(self, plumber_id, n):
        p = self.plumbers[plumber_id]
        if p.get("trial_stage") is None:
            p["trial_stage"] = n
        return p["trial_stage"]

    # state
    def get_plumber_state(self, plumber_id):
        return copy.deepcopy(self.states.get(plumber_id))

    def set_plumber_state(self, plumber_id, step, job_id=None, options=None, tries=0):
        self.states[plumber_id] = {
            "plumber_id": plumber_id, "step": step, "job_id": job_id,
            "options": options, "tries": tries,
        }

    def clear_plumber_state(self, plumber_id):
        self.states.pop(plumber_id, None)

    # holds
    def add_hold(self, plumber_id, reason):
        self.holds.add((plumber_id, reason))

    def clear_hold(self, plumber_id, reason):
        self.holds.discard((plumber_id, reason))

    def get_holds(self, plumber_id):
        return {r for p, r in self.holds if p == plumber_id}

    # invoices
    def insert_invoice(self, row):
        row = copy.deepcopy(row)
        row.setdefault("id", str(uuid.uuid4()))
        self.invoices[row["id"]] = row
        return copy.deepcopy(row)

    def get_invoice(self, invoice_id):
        i = self.invoices.get(invoice_id)
        return copy.deepcopy(i) if i else None

    def update_invoice(self, invoice_id, **fields):
        self.invoices[invoice_id].update(fields)
        return copy.deepcopy(self.invoices[invoice_id])

    def list_invoices(self, *, plumber_id=None, statuses=None):
        return [
            copy.deepcopy(i) for i in self.invoices.values()
            if (plumber_id is None or i["plumber_id"] == plumber_id)
            and (statuses is None or i["status"] in statuses)
        ]

    # audit
    def log_event(self, kind, *, job_id=None, plumber_id=None, payload=None):
        self.events.append(
            {"kind": kind, "job_id": job_id, "plumber_id": plumber_id, "payload": payload}
        )

    def has_event(self, plumber_id, kind):
        return any(e["plumber_id"] == plumber_id and e["kind"] == kind for e in self.events)

