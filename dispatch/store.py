"""Dispatch storage. The stores here extend the verification stores, so one object
serves both services:

    store = PostgresDispatchStore(DATABASE_URL)       # production
    store = InMemoryDispatchStore()                   # tests

Extra methods on top of the verification Store protocol:

    next_lead_ref() -> "L-0412"
    insert_lead(row) / get_lead(ref) / update_lead(ref, **f) / list_leads(statuses)
    claim_lead(ref, plumber_id, now) -> bool        atomic: exactly one caller wins
    add_offer(lead_ref, plumber_id, offered_at) -> (offer, created)
    list_offers(lead_ref=, plumber_id=, statuses=) / update_offer(offer_id, status)
"""

from __future__ import annotations

import copy
import uuid

from verification.store import InMemoryStore

LEAD_COLUMNS = frozenset({
    "id", "ref", "customer_phone", "customer_name", "suburb", "keyword", "address",
    "summary", "status", "taken_by", "taken_at", "telegram_message_id",
    "no_taker_alert_at", "created_at",
})
OPEN_LEAD = ("open", "offered")


class InMemoryDispatchStore(InMemoryStore):
    def __init__(self) -> None:
        super().__init__()
        self.leads: dict[str, dict] = {}       # by ref
        self.offers: dict[str, dict] = {}      # by id
        self._seq = 0

    def next_lead_ref(self) -> str:
        self._seq += 1
        return f"L-{self._seq:04d}"

    def insert_lead(self, row: dict) -> dict:
        row = copy.deepcopy(row)
        row.setdefault("id", str(uuid.uuid4()))
        for k in ("customer_name", "suburb", "keyword", "address", "summary", "taken_by",
                  "taken_at", "telegram_message_id", "no_taker_alert_at"):
            row.setdefault(k, None)
        row.setdefault("status", "open")
        self.leads[row["ref"]] = row
        return copy.deepcopy(row)

    def get_lead(self, ref):
        l = self.leads.get(ref)
        return copy.deepcopy(l) if l else None

    def update_lead(self, ref, **fields):
        self.leads[ref].update(fields)
        return copy.deepcopy(self.leads[ref])

    def list_leads(self, *, statuses=None):
        return [copy.deepcopy(l) for l in self.leads.values()
                if statuses is None or l["status"] in statuses]

    def claim_lead(self, ref, plumber_id, now) -> bool:
        lead = self.leads.get(ref)
        if not lead or lead["status"] not in OPEN_LEAD:
            return False
        lead.update(status="taken", taken_by=plumber_id, taken_at=now)
        return True

    def add_offer(self, lead_ref, plumber_id, offered_at):
        for o in self.offers.values():
            if o["lead_ref"] == lead_ref and o["plumber_id"] == plumber_id:
                return copy.deepcopy(o), False
        o = {"id": str(uuid.uuid4()), "lead_ref": lead_ref, "plumber_id": plumber_id,
             "status": "offered", "offered_at": offered_at}
        self.offers[o["id"]] = o
        return copy.deepcopy(o), True

    def list_offers(self, *, lead_ref=None, plumber_id=None, statuses=None):
        out = [copy.deepcopy(o) for o in self.offers.values()
               if (lead_ref is None or o["lead_ref"] == lead_ref)
               and (plumber_id is None or o["plumber_id"] == plumber_id)
               and (statuses is None or o["status"] in statuses)]
        return sorted(out, key=lambda o: o["offered_at"])

    def update_offer(self, offer_id, status):
        self.offers[offer_id]["status"] = status
