"""Queries the bot needs beyond the verification and dispatch packages: the plumber
registry, customer status, and the lookups behind the founder's Telegram commands."""

from __future__ import annotations

import re

from psycopg import sql

from dispatch.phone import normalize_phone
from dispatch.postgres_store import PostgresDispatchStore

PLUMBER_INSERT = frozenset({
    "first_name", "whatsapp", "experience_years", "pirb_registered",
    "services", "areas", "availability",
})
PLUMBER_EDITABLE = frozenset({"areas", "services", "availability", "first_name"})
_ID_PREFIX = re.compile(r"^[0-9a-f-]{4,36}$")


class AppStore(PostgresDispatchStore):
    # ───────────── plumber registry ─────────────

    def find_plumber_by_whatsapp(self, phone: str) -> dict | None:
        """Matches on normalised digits, so 082 / +27 / 27 formats all find the same row."""
        want = normalize_phone(phone)
        if not want:
            return None
        for p in self._run("SELECT * FROM plumbers", many=True):
            if normalize_phone(p.get("whatsapp")) == want:
                return p
        return None

    def list_plumbers(self) -> list[dict]:
        return self._run("SELECT * FROM plumbers ORDER BY created_at", many=True)

    def insert_plumber(self, row: dict) -> dict:
        return self._insert("plumbers", row, PLUMBER_INSERT)

    def update_plumber(self, plumber_id: str, **fields) -> dict | None:
        return self._update("plumbers", plumber_id, fields, PLUMBER_EDITABLE)

    def open_job_count(self, plumber_id: str) -> int:
        row = self._run(
            "SELECT count(*) AS n FROM job_lifecycle "
            "WHERE plumber_id = %s AND status IN ('accepted', 'awaiting_customer')",
            [plumber_id], one=True)
        return row["n"]

    # ───────────── customers ─────────────

    def latest_request_for_customer(self, phone: str) -> dict | None:
        return self._run(
            "SELECT l.ref, l.status AS lead_status, l.suburb, l.keyword, l.summary, "
            "       j.status AS job_status "
            "FROM dispatch_leads l LEFT JOIN job_lifecycle j ON j.lead_id = l.ref "
            "WHERE l.customer_phone = %s ORDER BY l.created_at DESC LIMIT 1",
            [phone], one=True)

    def customer_has_job(self, phone: str) -> bool:
        return self._run(
            "SELECT 1 AS found FROM job_lifecycle WHERE customer_phone = %s LIMIT 1",
            [phone], one=True) is not None

    # ───────────── founder / admin ─────────────

    def counts(self) -> dict:
        leads = self._run("SELECT status, count(*) AS n FROM dispatch_leads GROUP BY status", many=True)
        jobs = self._run("SELECT status, count(*) AS n FROM job_lifecycle GROUP BY status", many=True)
        return {"leads": {r["status"]: r["n"] for r in leads},
                "jobs": {r["status"]: r["n"] for r in jobs}}

    def _by_prefix(self, table: str, prefix: str) -> dict | None:
        """Founder types the first few characters of a uuid; ambiguous or malformed gives None."""
        prefix = (prefix or "").strip().lower()
        if not _ID_PREFIX.match(prefix):
            return None
        rows = self._run(
            sql.SQL("SELECT * FROM {} WHERE id::text LIKE %s LIMIT 2").format(sql.Identifier(table)),
            [prefix + "%"], many=True)
        return rows[0] if len(rows) == 1 else None

    def find_plumber_by_prefix(self, prefix: str) -> dict | None:
        return self._by_prefix("plumbers", prefix)

    def list_pending_plumbers(self) -> list[dict]:
        """Plumbers who finished onboarding and are waiting for /approve, oldest first."""
        return self._run(
            "SELECT p.* FROM plumbers p JOIN plumber_holds h ON h.plumber_id = p.id::text "
            "WHERE h.reason = 'pending_approval' ORDER BY p.created_at", many=True)

    def find_invoice_by_prefix(self, prefix: str) -> dict | None:
        return self._by_prefix("invoices", prefix)

    def find_job_by_prefix(self, prefix: str) -> dict | None:
        return self._by_prefix("job_lifecycle", prefix)

    def list_unpaid_invoices(self) -> list[dict]:
        return self._run(
            "SELECT i.*, p.first_name FROM invoices i "
            "LEFT JOIN plumbers p ON p.id::text = i.plumber_id "
            "WHERE i.status = 'sent' ORDER BY i.sent_at", many=True)

    def list_open_disputes(self) -> list[dict]:
        return self._run(
            "SELECT j.*, p.first_name FROM job_lifecycle j "
            "LEFT JOIN plumbers p ON p.id::text = j.plumber_id "
            "WHERE j.status IN ('disputed', 'unresolved') ORDER BY j.accepted_at", many=True)
