"""PostgresStore: the Store implementation for plain Postgres, built for Neon.

Works on any Postgres (Neon, Supabase's database, RDS, local). It talks SQL
directly through psycopg 3, so it needs no Supabase client.

    pip install "psycopg[binary,pool]>=3.2"

    from verification.postgres_store import PostgresStore
    store = PostgresStore(os.environ["DATABASE_URL"])
    svc = VerificationService(store, messenger)

Neon notes
----------
* Use the *pooled* connection string (host contains "-pooler") for the bot.
  Use the direct string only when applying migrations.
* `sslmode=require` must be in the URL. Neon includes it by default.
* Pooled connections run PgBouncer in transaction mode, which breaks
  server-side prepared statements, so `prepare_threshold=None` is set below.
* A suspended Neon compute takes a moment to wake. The first connection after
  idle can fail or time out, so every statement retries with backoff, and the
  pool health-checks connections it hands out (stale ones left over from a
  suspend are discarded). If the cold start matters on customer-facing paths,
  turn off auto-suspend for the compute (paid plans).
* Retries only cover connection-level failures. Statements here are idempotent
  or protected by unique keys (job_lifecycle.lead_id, invoices
  (plumber_id, week_start)), so an ambiguous retry cannot double-charge.

Everything is autocommit: each Store method is a single statement, matching the
Store contract. The service never relies on multi-statement transactions.
"""

from __future__ import annotations

import time
import uuid
from typing import Any

import psycopg
from psycopg import sql
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from psycopg_pool import ConnectionPool, PoolTimeout

JOB_COLUMNS = frozenset({
    "id", "lead_id", "plumber_id", "customer_phone", "customer_name", "suburb",
    "job_keyword", "accepted_at", "plumber_claim", "plumber_claim_at",
    "job_value_cents", "no_go_reason", "customer_response", "customer_response_at",
    "status", "fee_cents", "flags", "charged_at", "resolved_at", "invoice_id",
    "a3_sent_at", "b1_sent_at", "b2_sent_at", "a7_sent_at", "created_at",
})
INVOICE_COLUMNS = frozenset({
    "id", "plumber_id", "week_start", "job_count", "total_cents", "status",
    "sent_at", "due_at", "reminded_at", "paused_at", "paid_at",
})

_RETRY_DELAYS = (0.0, 0.5, 1.5, 3.0)   # covers a typical Neon wake-up


def _clean(row: dict | None) -> dict | None:
    """The service treats ids as strings; psycopg returns uuid.UUID."""
    if row is None:
        return None
    return {k: (str(v) if isinstance(v, uuid.UUID) else v) for k, v in row.items()}


class PostgresStore:
    def __init__(
        self,
        dsn: str,
        *,
        plumbers_table: str = "plumbers",
        plumbers_active_column: str | None = "active",
        plumbers_trial_column: str | None = "trial_stage",
        min_size: int = 1,
        max_size: int = 5,
        pool: ConnectionPool | None = None,
    ) -> None:
        """
        plumbers_table / plumbers_active_column: point these at your real table.
        The service needs `id` and `first_name`; the whole row is passed to your
        Messenger, so any contact column you need comes along. Pass
        plumbers_active_column=None if your table has no active flag.
        """
        self._plumbers = sql.Identifier(plumbers_table)
        self._active = sql.Identifier(plumbers_active_column) if plumbers_active_column else None
        self._trial = sql.Identifier(plumbers_trial_column) if plumbers_trial_column else None
        self.pool = pool or ConnectionPool(
            dsn,
            min_size=min_size,
            max_size=max_size,
            kwargs={"autocommit": True, "row_factory": dict_row, "prepare_threshold": None},
            check=ConnectionPool.check_connection,
            max_idle=240,        # recycle before Neon's idle suspend
            timeout=20,
            open=True,
        )

    def close(self) -> None:
        self.pool.close()

    # ───────────────────────── plumbing ─────────────────────────

    def _run(self, query, params=None, *, one: bool = False, many: bool = False):
        last: Exception | None = None
        for delay in _RETRY_DELAYS:
            if delay:
                time.sleep(delay)
            try:
                with self.pool.connection() as conn:
                    cur = conn.execute(query, params)
                    if one:
                        return _clean(cur.fetchone())
                    if many:
                        return [_clean(r) for r in cur.fetchall()]
                    return None
            except (psycopg.OperationalError, PoolTimeout) as e:   # cold start / dropped link
                last = e
        raise last  # type: ignore[misc]

    def _insert(self, table: str, row: dict, allowed: frozenset[str]) -> dict:
        unknown = set(row) - allowed
        if unknown:
            raise ValueError(f"unknown {table} columns: {sorted(unknown)}")
        cols = list(row)
        q = sql.SQL("INSERT INTO {} ({}) VALUES ({}) RETURNING *").format(
            sql.Identifier(table),
            sql.SQL(", ").join(sql.Identifier(c) for c in cols),
            sql.SQL(", ").join([sql.Placeholder()] * len(cols)),
        )
        return self._run(q, [row[c] for c in cols], one=True)

    def _update(self, table: str, row_id: str, fields: dict, allowed: frozenset[str]) -> dict | None:
        unknown = set(fields) - allowed
        if unknown:
            raise ValueError(f"unknown {table} columns: {sorted(unknown)}")
        if not fields:
            return self._run(
                sql.SQL("SELECT * FROM {} WHERE id = %s").format(sql.Identifier(table)),
                [row_id], one=True)
        cols = list(fields)
        q = sql.SQL("UPDATE {} SET {} WHERE id = %s RETURNING *").format(
            sql.Identifier(table),
            sql.SQL(", ").join(
                sql.SQL("{} = {}").format(sql.Identifier(c), sql.Placeholder()) for c in cols),
        )
        return self._run(q, [fields[c] for c in cols] + [row_id], one=True)

    # ───────────────────────── jobs ─────────────────────────

    def insert_job(self, row: dict) -> dict:
        return self._insert("job_lifecycle", row, JOB_COLUMNS)

    def get_job(self, job_id: str) -> dict | None:
        return self._run("SELECT * FROM job_lifecycle WHERE id = %s", [job_id], one=True)

    def get_job_by_lead(self, lead_id: str) -> dict | None:
        return self._run("SELECT * FROM job_lifecycle WHERE lead_id = %s", [lead_id], one=True)

    def update_job(self, job_id: str, **fields: Any) -> dict:
        return self._update("job_lifecycle", job_id, fields, JOB_COLUMNS)

    def list_jobs(
        self, *, statuses=None, plumber_id=None, customer_phone=None
    ) -> list[dict]:
        where, params = [], []
        if statuses is not None:
            where.append("status = ANY(%s)")
            params.append(list(statuses))
        if plumber_id is not None:
            where.append("plumber_id = %s")
            params.append(plumber_id)
        if customer_phone is not None:
            where.append("customer_phone = %s")
            params.append(customer_phone)
        q = "SELECT * FROM job_lifecycle"
        if where:
            q += " WHERE " + " AND ".join(where)
        q += " ORDER BY accepted_at, id"
        return self._run(q, params, many=True)

    # ───────────────────────── plumbers ─────────────────────────

    def get_plumber(self, plumber_id: str) -> dict | None:
        q = sql.SQL("SELECT * FROM {} WHERE id = %s").format(self._plumbers)
        return self._run(q, [plumber_id], one=True)

    def list_active_plumbers(self) -> list[dict]:
        if self._active is None:
            q = sql.SQL("SELECT * FROM {}").format(self._plumbers)
        else:
            q = sql.SQL("SELECT * FROM {} WHERE {} IS TRUE").format(self._plumbers, self._active)
        return self._run(q, many=True)

    # ───────────────────────── trial leads ─────────────────────────
    # plumbers.trial_stage is the number of trial leads still free. NULL means never granted.

    def use_trial_lead(self, plumber_id: str) -> int | None:
        """Uses one trial lead. Returns how many are left, or None if there was none to use.
        One UPDATE, so two jobs taken at once can never use the same lead."""
        if self._trial is None:
            return None
        q = sql.SQL("UPDATE {t} SET {c} = {c} - 1 WHERE id = %s AND {c} > 0 RETURNING {c} AS left_over").format(
            t=self._plumbers, c=self._trial)
        row = self._run(q, [plumber_id], one=True)
        return row["left_over"] if row else None

    def grant_trial_leads(self, plumber_id: str, n: int) -> int:
        """Gives n trial leads to a plumber who has never had any (NULL). Anyone who already has
        a number, including 0 for "used them all", keeps it. Returns the number they now have."""
        if self._trial is None:
            return 0
        q = sql.SQL("UPDATE {t} SET {c} = %s WHERE id = %s AND {c} IS NULL RETURNING {c} AS now_has").format(
            t=self._plumbers, c=self._trial)
        row = self._run(q, [n, plumber_id], one=True)
        if row:
            return row["now_has"]
        row = self._run(sql.SQL("SELECT {c} AS now_has FROM {t} WHERE id = %s").format(
            t=self._plumbers, c=self._trial), [plumber_id], one=True)
        return (row or {}).get("now_has") or 0

    # ───────────────────────── conversation state ─────────────────────────

    def get_plumber_state(self, plumber_id: str) -> dict | None:
        return self._run("SELECT * FROM plumber_state WHERE plumber_id = %s", [plumber_id], one=True)

    def set_plumber_state(
        self, plumber_id: str, step: str, job_id: str | None = None,
        options: list[str] | None = None, tries: int = 0,
    ) -> None:
        self._run(
            """
            INSERT INTO plumber_state (plumber_id, step, job_id, options, tries, updated_at)
            VALUES (%s, %s, %s, %s, %s, now())
            ON CONFLICT (plumber_id) DO UPDATE SET
              step = EXCLUDED.step, job_id = EXCLUDED.job_id,
              options = EXCLUDED.options, tries = EXCLUDED.tries, updated_at = now()
            """,
            [plumber_id, step, job_id, Jsonb(options) if options is not None else None, tries],
        )

    def clear_plumber_state(self, plumber_id: str) -> None:
        self._run("DELETE FROM plumber_state WHERE plumber_id = %s", [plumber_id])

    # ───────────────────────── holds ─────────────────────────

    def add_hold(self, plumber_id: str, reason: str) -> None:
        self._run(
            "INSERT INTO plumber_holds (plumber_id, reason) VALUES (%s, %s) ON CONFLICT DO NOTHING",
            [plumber_id, reason])

    def clear_hold(self, plumber_id: str, reason: str) -> None:
        self._run("DELETE FROM plumber_holds WHERE plumber_id = %s AND reason = %s",
                  [plumber_id, reason])

    def get_holds(self, plumber_id: str) -> set[str]:
        rows = self._run("SELECT reason FROM plumber_holds WHERE plumber_id = %s",
                         [plumber_id], many=True)
        return {r["reason"] for r in rows}

    # ───────────────────────── invoices ─────────────────────────

    def insert_invoice(self, row: dict) -> dict:
        return self._insert("invoices", row, INVOICE_COLUMNS)

    def get_invoice(self, invoice_id: str) -> dict | None:
        return self._run("SELECT * FROM invoices WHERE id = %s", [invoice_id], one=True)

    def update_invoice(self, invoice_id: str, **fields: Any) -> dict:
        return self._update("invoices", invoice_id, fields, INVOICE_COLUMNS)

    def list_invoices(self, *, plumber_id=None, statuses=None) -> list[dict]:
        where, params = [], []
        if plumber_id is not None:
            where.append("plumber_id = %s")
            params.append(plumber_id)
        if statuses is not None:
            where.append("status = ANY(%s)")
            params.append(list(statuses))
        q = "SELECT * FROM invoices"
        if where:
            q += " WHERE " + " AND ".join(where)
        q += " ORDER BY week_start, id"
        return self._run(q, params, many=True)

    # ───────────────────────── audit ─────────────────────────

    def log_event(
        self, kind: str, *, job_id: str | None = None, plumber_id: str | None = None,
        payload: dict | None = None,
    ) -> None:
        self._run(
            "INSERT INTO job_events (job_id, plumber_id, kind, payload) VALUES (%s, %s, %s, %s)",
            [job_id, plumber_id, kind, Jsonb(payload) if payload is not None else None])

    def has_event(self, plumber_id: str, kind: str) -> bool:
        row = self._run(
            "SELECT 1 AS found FROM job_events WHERE plumber_id = %s AND kind = %s LIMIT 1",
            [plumber_id, kind], one=True)
        return row is not None
