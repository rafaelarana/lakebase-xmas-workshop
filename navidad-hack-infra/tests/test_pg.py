from types import SimpleNamespace

import psycopg2
import pytest

import pg
from pg import BranchGone, Pg


class NotFound(Exception):
    pass


class FakeCursor:
    def __init__(self, conn):
        self.conn, self.description, self.rowcount = conn, None, 0

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, params=None):
        self.conn.log.append(sql)
        if self.conn.drop_next:
            self.conn.drop_next, self.conn.closed = False, 2
            raise psycopg2.OperationalError("SSL connection has been closed unexpectedly")
        if sql == "BAD":
            raise psycopg2.errors.UndefinedTable("relation does not exist") if hasattr(psycopg2, "errors") else psycopg2.ProgrammingError()
        self.description, self.rowcount = [("x",)], 7

    def fetchall(self):
        return [(1,)]

    def copy_expert(self, sql, buf):
        buf.write("1\t2\n")


class FakeConn:
    def __init__(self, n):
        self.n, self.closed, self.autocommit, self.drop_next = n, 0, False, False
        self.log, self.commits, self.rollbacks = [], 0, 0

    def cursor(self):
        return FakeCursor(self)

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1

    def close(self):
        self.closed = 1


class FakePostgres:
    def __init__(self, gone=False):
        self.gone = gone

    def list_endpoints(self, parent):
        if self.gone:
            raise NotFound(parent)
        return [SimpleNamespace(name=f"{parent}/endpoints/primary", status=SimpleNamespace(endpoint_type="READ_WRITE"))]

    def get_endpoint(self, name):
        return SimpleNamespace(status=SimpleNamespace(hosts=SimpleNamespace(host="h")))

    def generate_database_credential(self, endpoint):
        return SimpleNamespace(token="t")

    def get_branch(self, name):
        if self.gone:
            raise NotFound(name)


@pytest.fixture
def conns(monkeypatch):
    made = []

    def fake_connect(**kw):
        assert kw["connect_timeout"] == 30 and kw["keepalives"] == 1
        made.append(FakeConn(len(made)))
        return made[-1]

    monkeypatch.setattr(pg.psycopg2, "connect", fake_connect)
    monkeypatch.setattr(pg.time, "sleep", lambda s: None)
    return made


def make(gone=False):
    return Pg(SimpleNamespace(postgres=FakePostgres(gone)), "me", "projects/p/branches/production", log=lambda *a: None)


def test_reconnects_once_when_connection_lost(conns):
    db = make()
    db.conn.drop_next = True
    assert db.q("SELECT 1") == [(1,)]
    assert len(conns) == 2 and conns[0].closed and not conns[1].closed


def test_sql_errors_are_not_retried(conns):
    db = make()
    with pytest.raises(psycopg2.Error):
        db.q("BAD")
    assert len(conns) == 1


def test_reconnects_after_idle_suspend(conns):
    db = make()
    db.conn.closed = 2  # compute suspended: psycopg2 marks the connection closed
    db.q("SELECT 1")
    assert len(conns) == 2


def test_transaction_rolls_back_on_error_and_restores_autocommit(conns):
    db = make()

    def boom(cur):
        cur.execute("BAD")

    with pytest.raises(psycopg2.Error):
        db.transaction(boom)
    c = conns[0]
    assert c.rollbacks == 1 and c.commits == 0 and c.autocommit is True and len(conns) == 1


def test_transaction_retried_on_fresh_connection_when_dropped(conns):
    db = make()
    db.conn.drop_next = True
    calls = []

    def work(cur):
        calls.append(1)
        cur.execute("INSERT")
        return cur.rowcount

    assert db.transaction(work) == 7
    assert len(calls) == 2 and conns[0].closed and conns[1].commits == 1


def test_copy_out(conns):
    assert make().copy_out("COPY t TO STDOUT") == "1\t2\n"


def test_deleted_branch_fails_fast(conns):
    with pytest.raises(BranchGone):
        make(gone=True).conn
    assert conns == []


def test_closed_on_purpose_is_not_reopened(conns):
    db = make()
    db.q("SELECT 1")
    db.close()
    with pytest.raises(RuntimeError, match="ya se cerró"):
        db.q("SELECT 1")
    assert len(conns) == 1
