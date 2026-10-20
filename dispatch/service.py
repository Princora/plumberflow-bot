"""DispatchService: lead arrives -> you tap a button in Telegram -> plumber gets a
WhatsApp offer -> plumber replies "take" -> exactly one plumber wins -> job created
and the verification clock starts.

Rules worth knowing:
* The offer carries suburb and problem only. Name, phone and address are sent on take.
* The first "take" wins, decided by one atomic UPDATE in the store.
* Paused plumbers are never offered to without an explicit "Offer anyway" from you.
* A lead nobody takes within offer_ttl_minutes is re-posted to you with the buttons.
"""

from __future__ import annotations

import re
from datetime import timedelta
from typing import Protocol

from verification import FEE_CENTS, VerificationService
from verification import templates as VT

from . import templates as T
from .phone import normalize_phone
from .suburbs import area_key

TAKE_RE = re.compile(
    r"^\s*(?:i['’]?ll\s+)?(?:take|taking|accept|yes|me)(?:\s+it)?(?:\s+(?:l-?)?(\d{1,6}))?[\s.,]*$",
    re.I)


class Telegram(Protocol):
    def post(self, text: str, buttons: list[list[dict]] | None = None) -> int: ...
    def edit(self, message_id: int, text: str, buttons: list[list[dict]] | None = None) -> None: ...


def _btn(text: str, data: str) -> dict:
    return {"text": text, "callback_data": data}      # callback_data must stay under 64 bytes


def _p8(plumber: dict) -> str:
    return str(plumber["id"]).replace("-", "")[:8]


class DispatchService:
    def __init__(
        self, store, verification: VerificationService, messenger, telegram: Telegram, *,
        offer_ttl_minutes: int = 30, max_broadcast: int = 3, fee_cents: int = FEE_CENTS,
    ) -> None:
        self.store, self.verification, self.msg, self.tg = store, verification, messenger, telegram
        self.ttl, self.max_broadcast, self.fee_cents = offer_ttl_minutes, max_broadcast, fee_cents

    def now(self):
        return self.verification.now()

    # ───────────────────────── intake ─────────────────────────

    def new_lead(
        self, *, customer_phone: str, customer_name: str | None, suburb: str | None,
        keyword: str | None, address: str | None = None, summary: str | None = None,
    ) -> dict:
        """Call from your existing bot when a homeowner's job is captured."""
        lead = self.store.insert_lead({
            "ref": self.store.next_lead_ref(), "customer_phone": customer_phone,
            "customer_name": customer_name, "suburb": suburb, "keyword": keyword,
            "address": address, "summary": summary, "status": "open",
            "created_at": self.now(),
        })
        text, buttons = self._founder_view(lead)
        mid = self.tg.post(text, buttons)
        return self.store.update_lead(lead["ref"], telegram_message_id=mid)

    # ───────────────────────── plumbers ─────────────────────────

    def find_plumber_by_phone(self, phone: str) -> dict | None:
        want = normalize_phone(phone)
        for p in self.store.list_active_plumbers():
            if want and normalize_phone(p.get("whatsapp")) == want:
                return p
        return None

    def _is_paused(self, plumber: dict) -> list[str]:
        return sorted(self.store.get_holds(plumber["id"]))

    def _matching(self, lead: dict) -> list[dict]:
        """Active, unpaused plumbers who cover the lead's suburb."""
        # Compare by canonical suburb, so "Rivonia Ext 3" finds the plumber who typed "rivonia".
        suburb = area_key(lead.get("suburb"))
        out = []
        for p in self.store.list_active_plumbers():
            areas = {area_key(a) for a in (p.get("areas") or [])}
            if suburb and suburb in areas and not self._is_paused(p):
                out.append(p)
        return out

    def _label(self, lead: dict) -> str:
        return " ".join(x for x in (lead.get("suburb"), lead.get("keyword")) if x) or lead["ref"]

    # ───────────────────────── Telegram side ─────────────────────────

    def _founder_view(self, lead: dict, note: str | None = None) -> tuple[str, list[list[dict]] | None]:
        text = T.founder_lead(lead)
        offers = self.store.list_offers(lead_ref=lead["ref"])
        names = []
        for o in offers:
            p = self.store.get_plumber(o["plumber_id"]) or {"first_name": "?"}
            names.append(p["first_name"] + (" (took it)" if o["status"] == "taken" else ""))
        if lead["status"] == "taken":
            taker = self.store.get_plumber(lead["taken_by"]) or {"first_name": "a plumber"}
            return f"{text}\n\nTaken by {taker['first_name']}. Job created.", None
        if lead["status"] == "skipped":
            return f"{text}\n\nSkipped.", None
        if names:
            text += f"\n\nOffered: {', '.join(names)}. Waiting."
        if note:
            text += f"\n\n{note}"
        ref = lead["ref"]
        rows: list[list[dict]] = []
        offered_ids = {o["plumber_id"] for o in offers}
        suggestions = [p for p in self._matching(lead) if p["id"] not in offered_ids]
        for p in suggestions[:3]:
            rows.append([_btn(f"Offer to {p['first_name']}", f"o:{ref}:{_p8(p)}")])
        if len(suggestions) >= 2:
            n = min(len(suggestions), self.max_broadcast)
            rows.append([_btn(f"Offer to {n} matching", f"m:{ref}")])
        rows.append([_btn("Pick a plumber", f"l:{ref}"), _btn("Skip", f"s:{ref}")])
        return text, rows

    def _refresh(self, ref: str, note: str | None = None) -> None:
        lead = self.store.get_lead(ref)
        if lead and lead.get("telegram_message_id"):
            text, buttons = self._founder_view(lead, note)
            self.tg.edit(lead["telegram_message_id"], text, buttons)

    def _plumber_by_p8(self, p8: str) -> dict | None:
        hits = [p for p in self.store.list_active_plumbers() if _p8(p) == p8]
        return hits[0] if len(hits) == 1 else None

    def on_telegram_callback(self, data: str) -> str:
        """Returns the short toast Telegram shows on the tapped button."""
        kind, _, rest = data.partition(":")
        ref, _, arg = rest.partition(":")
        lead = self.store.get_lead(ref)
        if not lead:
            return "Lead not found"
        if lead["status"] in ("taken", "skipped") and kind in ("o", "f", "m", "l", "s"):
            return "Already " + lead["status"]
        if kind in ("o", "f"):
            plumber = self._plumber_by_p8(arg)
            if not plumber:
                return "Plumber not found"
            holds = self._is_paused(plumber)
            if holds and kind == "o":
                text = (f"{T.founder_lead(lead)}\n\n{plumber['first_name']} is paused "
                        f"({', '.join(holds)}). Offer anyway?")
                self.tg.edit(lead["telegram_message_id"], text, [
                    [_btn("Offer anyway", f"f:{ref}:{arg}")], [_btn("Back", f"b:{ref}")]])
                return "Paused"
            self._send_offers(lead, [plumber])
            return f"Offered to {plumber['first_name']}"
        if kind == "m":
            targets = self._matching(lead)
            have = {o["plumber_id"] for o in self.store.list_offers(lead_ref=ref)}
            targets = [p for p in targets if p["id"] not in have][: self.max_broadcast]
            if not targets:
                return "No one to offer to"
            self._send_offers(lead, targets)
            return f"Offered to {len(targets)}"
        if kind == "l":
            have = {o["plumber_id"] for o in self.store.list_offers(lead_ref=ref)}
            rows = [[_btn(p["first_name"] + (" (paused)" if self._is_paused(p) else ""),
                          f"o:{ref}:{_p8(p)}")]
                    for p in self.store.list_active_plumbers() if p["id"] not in have][:10]
            rows.append([_btn("Back", f"b:{ref}")])
            self.tg.edit(lead["telegram_message_id"], T.founder_lead(lead), rows)
            return "Pick one"
        if kind == "b":
            self._refresh(ref)
            return ""
        if kind == "s":
            self.store.update_lead(ref, status="skipped")
            self._refresh(ref)
            return "Skipped"
        return "Unknown action"

    def _send_offers(self, lead: dict, plumbers: list[dict]) -> None:
        ref = lead["ref"]
        fresh = []
        for p in plumbers:
            _, created = self.store.add_offer(ref, p["id"], self.now())
            if created:
                fresh.append(p)
        if not fresh:
            return
        self.store.update_lead(ref, status="offered", no_taker_alert_at=None)
        n = len(self.store.list_offers(lead_ref=ref, statuses=("offered",)))
        for p in fresh:
            # A plumber with a trial lead left sees "no fee"; the lead is only used if they take it.
            fee = 0 if (p.get("trial_stage") or 0) > 0 else self.fee_cents
            self.msg.send_plumber(p, T.offer_to_plumber(
                ref, lead.get("suburb") or "Johannesburg",
                lead.get("summary") or lead.get("keyword") or "Plumbing job", n, fee))
        self._refresh(ref)

    # ───────────────────────── plumber replies ─────────────────────────

    def try_take(self, plumber: dict, text: str) -> bool:
        """True when the message was a take attempt we handled. False falls through."""
        m = TAKE_RE.match(text or "")
        if not m:
            return False
        want_ref = f"L-{int(m.group(1)):04d}" if m.group(1) else None

        open_offers = []
        for o in self.store.list_offers(plumber_id=plumber["id"], statuses=("offered",)):
            lead = self.store.get_lead(o["lead_ref"])
            if lead and lead["status"] in ("open", "offered"):
                open_offers.append((o, lead))

        if want_ref:
            chosen = next(((o, l) for o, l in open_offers if l["ref"] == want_ref), None)
            if chosen is None:
                lead = self.store.get_lead(want_ref)
                if lead and self.store.list_offers(lead_ref=want_ref, plumber_id=plumber["id"]):
                    self.msg.send_plumber(plumber, T.gone(self._label(lead)))
                    return True
                return False
        elif not open_offers:
            # Replying just after losing a race: say so. Otherwise "yes" with nothing open
            # is probably an answer to something else, so fall through.
            for o in reversed(self.store.list_offers(plumber_id=plumber["id"], statuses=("lost",))):
                lost = self.store.get_lead(o["lead_ref"])
                if lost and lost.get("taken_at") and (self.now() - lost["taken_at"]) <= timedelta(minutes=15):
                    self.msg.send_plumber(plumber, T.gone(self._label(lost)))
                    return True
            return False
        elif len(open_offers) > 1:
            self.msg.send_plumber(plumber, T.which_offer(
                [(l["ref"], self._label(l)) for _, l in open_offers]))
            return True
        else:
            chosen = open_offers[0]

        offer, lead = chosen
        if not self.store.claim_lead(lead["ref"], plumber["id"], self.now()):
            self.store.update_offer(offer["id"], "lost")
            self.msg.send_plumber(plumber, T.gone(self._label(lead)))
            return True

        self.store.update_offer(offer["id"], "taken")
        job = self.verification.register_acceptance(
            lead_id=lead["ref"], plumber_id=plumber["id"], customer_phone=lead["customer_phone"],
            customer_name=lead.get("customer_name"), suburb=lead.get("suburb"),
            keyword=lead.get("keyword"))
        take_text = VT.take_confirmed(
            lead.get("customer_name") or "Customer", lead["customer_phone"],
            lead.get("address") or "Address: ask the customer")
        if job and "trial_lead" in (job.get("flags") or []):
            left = (self.store.get_plumber(plumber["id"]) or {}).get("trial_stage") or 0
            take_text += "\n\n" + VT.trial_lead_taken(left)
        self.msg.send_plumber(plumber, take_text)
        for other in self.store.list_offers(lead_ref=lead["ref"], statuses=("offered",)):
            self.store.update_offer(other["id"], "lost")
            p = self.store.get_plumber(other["plumber_id"])
            if p:
                self.msg.send_plumber(p, T.lost_to_other(self._label(lead)))
        self._refresh(lead["ref"])
        return job is not None

    def on_whatsapp_text(self, phone: str, text: str) -> bool:
        """One entry point for inbound plumber WhatsApp text: take first, then the
        verification flow (done / no go / amounts). False = not a plumber or unhandled."""
        plumber = self.find_plumber_by_phone(phone)
        if not plumber:
            return False
        if self.try_take(plumber, text):
            return True
        return self.verification.on_plumber_message(plumber["id"], text)

    # ───────────────────────── sweeps ─────────────────────────

    def run_sweeps(self) -> None:
        """Re-surface leads nobody has taken. Call alongside verification.run_sweeps()."""
        for lead in self.store.list_leads(statuses=("offered",)):
            if lead.get("no_taker_alert_at"):
                continue
            offers = self.store.list_offers(lead_ref=lead["ref"], statuses=("offered",))
            if not offers:
                continue
            age_min = (self.now() - max(o["offered_at"] for o in offers)).total_seconds() / 60
            if age_min < self.ttl:
                continue
            self.store.update_lead(lead["ref"], no_taker_alert_at=self.now())
            text, buttons = self._founder_view(
                lead, f"No reply in {self.ttl} minutes. Offers are still open.")
            mid = self.tg.post(text, buttons)
            self.store.update_lead(lead["ref"], telegram_message_id=mid)
