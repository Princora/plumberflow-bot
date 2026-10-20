"""Fixtures. Every flow test runs against the in-memory store and, when a
database is available, against real Postgres through PostgresStore.

Postgres is opt-in: set PLUMBERFLOW_TEST_DSN to any Postgres you can write to
(a local server, or a throwaway Neon branch). Everything the tests create lives
in a dedicated schema called pf_test, which is dropped and rebuilt at the start
of the run. Nothing outside that schema is touched.
"""

import os
import pathlib
from urllib.parse import quote

import pytest

from dispatch.postgres_store import PostgresDispatchStore
from dispatch.store import InMemoryDispatchStore

MIGRATIONS = [pathlib.Path(__file__).resolve().parent.parent / "migrations" / n
              for n in ("001_job_lifecycle.sql", "002_dispatch.sql", "005_credits.sql")]
SCHEMA = "pf_test"
TABLES = ("job_lifecycle, invoices, plumber_state, plumber_holds, job_events, plumbers, "
          "dispatch_offers, dispatch_leads, credit_orders")

# Stand-in for your real plumbers table (the migration assumes it already exists).
PLUMBERS_DDL = """
create table plumbers (
  id uuid primary key default gen_random_uuid(),
  first_name text not null,
  active boolean not null default true,
  whatsapp text,
  areas text[],
  trial_stage integer
);
"""


class PgTestStore(PostgresDispatchStore):
    """PostgresStore plus the plumber-seeding helper the tests use."""

    def add_plumber(self, first_name, *, active=True, **extra):
        return self._run(
            "insert into plumbers (first_name, active, whatsapp, areas, trial_stage, credits) values (%s, %s, %s, %s, %s, %s) returning *",
            [first_name, active, extra.get("whatsapp"), extra.get("areas"), extra.get("trial_stage"), extra.get("credits") or 0], one=True)


def _scoped(dsn: str) -> str:
    sep = "&" if "?" in dsn else "?"
    return f"{dsn}{sep}options={quote(f'-c search_path={SCHEMA}')}"


@pytest.fixture(scope="session")
def pg_store():
    dsn = os.environ.get("PLUMBERFLOW_TEST_DSN")
    if not dsn:
        pytest.skip("set PLUMBERFLOW_TEST_DSN to run the Postgres store tests")
    import psycopg
    with psycopg.connect(dsn, autocommit=True) as c:
        c.execute(f"drop schema if exists {SCHEMA} cascade")
        c.execute(f"create schema {SCHEMA}")
        c.execute(f"set search_path = {SCHEMA}")
        c.execute(PLUMBERS_DDL)
        for m in MIGRATIONS:
            c.execute(m.read_text())
    store = PgTestStore(_scoped(dsn), min_size=1, max_size=3)
    yield store
    store.close()


@pytest.fixture(params=["memory", "postgres"])
def store(request):
    if request.param == "memory":
        return InMemoryDispatchStore()
    pg = request.getfixturevalue("pg_store")
    pg._run(f"truncate {TABLES}")
    return pg
