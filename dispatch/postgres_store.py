"""PostgresDispatchStore: dispatch storage on Postgres/Neon. Needs psycopg."""

from __future__ import annotations

from typing import Any

from verification.postgres_store import PostgresStore

from .store import LEAD_COLUMNS


class PostgresDispatchStore(PostgresStore):
    def next_lead_ref(self) -> str:
        row = self._run("SELECT nextval('dispatch_lead_seq') AS n", one=True)
        return f"L-{row['n']:04d}"

    def insert_lead(self, row: dict) -> dict:
        return self._insert("dispatch_leads", row, LEAD_COLUMNS)

    def get_lead(self, ref):
        return self._run("SELECT * FROM dispatch_leads WHERE ref = %s", [ref], one=True)

    def update_lead(self, ref, **fields: Any):
        unknown = set(fields) - LEAD_COLUMNS
        if unknown:
            raise ValueError(f"unknown dispatch_leads columns: {sorted(unknown)}")
        from psycopg import sql
        cols = list(fields)
        q = sql.SQL("UPDATE dispatch_leads SET {} WHERE ref = %s RETURNING *").format(
            sql.SQL(", ").join(sql.SQL("{} = {}").format(sql.Identifier(c), sql.Placeholder())
                               for c in cols))
        return self._run(q, [fields[c] for c in cols] + [ref], one=True)

    def list_leads(self, *, statuses=None):
        if statuses is None:
            return self._run("SELECT * FROM dispatch_leads ORDER BY created_at", many=True)
        return self._run("SELECT * FROM dispatch_leads WHERE status = ANY(%s) ORDER BY created_at",
                         [list(statuses)], many=True)

    def claim_lead(self, ref, plumber_id, now) -> bool:
        # One statement, so two simultaneous takes cannot both succeed.
        row = self._run(
            "UPDATE dispatch_leads SET status='taken', taken_by=%s, taken_at=%s "
            "WHERE ref=%s AND status IN ('open','offered') RETURNING ref",
            [plumber_id, now, ref], one=True)
        return row is not None

    def add_offer(self, lead_ref, plumber_id, offered_at):
        row = self._run(
            "INSERT INTO dispatch_offers (lead_ref, plumber_id, offered_at) VALUES (%s,%s,%s) "
            "ON CONFLICT (lead_ref, plumber_id) DO NOTHING RETURNING *",
            [lead_ref, plumber_id, offered_at], one=True)
        if row is not None:
            return row, True
        existing = self._run(
            "SELECT * FROM dispatch_offers WHERE lead_ref=%s AND plumber_id=%s",
            [lead_ref, plumber_id], one=True)
        return existing, False

    def list_offers(self, *, lead_ref=None, plumber_id=None, statuses=None):
        where, params = [], []
        if lead_ref is not None:
            where.append("lead_ref = %s"); params.append(lead_ref)
        if plumber_id is not None:
            where.append("plumber_id = %s"); params.append(plumber_id)
        if statuses is not None:
            where.append("status = ANY(%s)"); params.append(list(statuses))
        q = "SELECT * FROM dispatch_offers"
        if where:
            q += " WHERE " + " AND ".join(where)
        return self._run(q + " ORDER BY offered_at, id", params, many=True)

    def update_offer(self, offer_id, status):
        self._run("UPDATE dispatch_offers SET status = %s WHERE id = %s", [status, offer_id])
