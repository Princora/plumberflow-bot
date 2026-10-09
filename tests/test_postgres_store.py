"""PostgresStore behaviour that does not need a database: Neon cold-start retry,
no retry on real errors, and column whitelisting."""

import uuid
from contextlib import contextmanager

import psycopg
import pytest

from verification import postgres_store as ps
from verification.postgres_store import PostgresStore


class _Cur:
    def __init__(self, row):
        self.row = row

    def fetchone(self):
        return self.row

    def fetchall(self):
        return [self.row]


class _Conn:
    def __init__(self, row):
        self.row = row

    def execute(self, query, params=None):
        return _Cur(self.row)


class ScriptedPool:
    """Raises the scripted errors in order, then hands out a working connection."""

    def __init__(self, errors, row=None):
        self.errors, self.calls, self.row = list(errors), 0, row

    @contextmanager
    def connection(self):
        self.calls += 1
        if self.errors:
            raise self.errors.pop(0)
        yield _Conn(self.row)


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    monkeypatch.setattr(ps.time, "sleep", lambda s: None)


def test_retries_through_a_neon_cold_start():
    jid = uuid.uuid4()
    pool = ScriptedPool([psycopg.OperationalError("connection refused")] * 2, row={"id": jid})
    store = PostgresStore("", pool=pool)
    job = store.get_job("whatever")
    assert job == {"id": str(jid)}          # uuid.UUID normalised to str
    assert pool.calls == 3


def test_gives_up_after_the_last_retry():
    pool = ScriptedPool([psycopg.OperationalError("down")] * 10)
    store = PostgresStore("", pool=pool)
    with pytest.raises(psycopg.OperationalError):
        store.get_job("x")
    assert pool.calls == len(ps._RETRY_DELAYS)


def test_real_errors_are_not_retried():
    pool = ScriptedPool([psycopg.errors.UniqueViolation("duplicate key")])
    store = PostgresStore("", pool=pool)
    with pytest.raises(psycopg.errors.UniqueViolation):
        store.insert_job({"lead_id": "L1", "plumber_id": "p", "customer_phone": "x"})
    assert pool.calls == 1


def test_unknown_columns_are_rejected_before_touching_the_database():
    pool = ScriptedPool([])
    store = PostgresStore("", pool=pool)
    with pytest.raises(ValueError):
        store.update_job("id", status="charged", evil="1; drop table x")
    with pytest.raises(ValueError):
        store.insert_invoice({"plumber_id": "p", "nope": 1})
    assert pool.calls == 0
