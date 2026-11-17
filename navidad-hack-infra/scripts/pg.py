"""Lakebase Postgres connection for notebooks that survives compute auto-suspend.

Run cell by cell with pauses, a branch compute auto-suspends (60 s idle by default) and drops
idle connections ("SSL connection has been closed unexpectedly"). ``Pg`` reconnects lazily and
retries a statement once when the *connection* was lost; any other database error is raised.
Statements passed to ``q`` must be safe to repeat (the workshop ones are).
"""
from __future__ import annotations

import io
import time
from typing import Any, Callable

import psycopg2

PG_DATABASE = "databricks_postgres"
CONNECT_KW = dict(dbname=PG_DATABASE, sslmode="require", connect_timeout=30,
                  keepalives=1, keepalives_idle=30, keepalives_interval=10, keepalives_count=3)


def _lost(exc: Exception, conn: Any) -> bool:
    """True when the error means the connection is gone (not a SQL/permission error)."""
    return isinstance(exc, (psycopg2.OperationalError, psycopg2.InterfaceError)) and bool(
        conn is None or conn.closed)


def _not_found(exc: Exception) -> bool:
    return any(k in type(exc).__name__ for k in ("NotFound", "DoesNotExist"))


class BranchGone(RuntimeError):
    pass


class Pg:
    def __init__(self, w: Any, user: str, branch_path: str, attempts: int = 10, log: Callable = print):
        self.w, self.user, self.branch_path, self.attempts, self.log = w, user, branch_path, attempts, log
        self.label = branch_path.rsplit("/", 1)[-1]
        self._conn: Any = None
        self._closed_by_user = False

    # -- connection ----------------------------------------------------------------------------
    def rw_endpoint(self) -> str:
        """Read-write compute of the branch. Child branches get «primary»; a root branch restored
        from a snapshot gets a generated name (ep-…), so look it up instead of assuming."""
        eps = list(self.w.postgres.list_endpoints(parent=self.branch_path))
        rw = [e for e in eps if "READ_ONLY" not in str(getattr(e.status, "endpoint_type", ""))]
        if not rw:
            raise RuntimeError(f"la rama {self.label} no tiene cómputo de lectura-escritura")
        return rw[0].name

    def _open(self) -> Any:
        last: Exception | None = None
        for i in range(self.attempts):
            try:
                ep = self.rw_endpoint()
                host = self.w.postgres.get_endpoint(name=ep).status.hosts.host
                token = self.w.postgres.generate_database_credential(endpoint=ep).token
                conn = psycopg2.connect(host=host, user=self.user, password=token, **CONNECT_KW)
                conn.autocommit = True
                return conn
            except Exception as exc:
                if _not_found(exc) and self._branch_gone():
                    raise BranchGone(f"la rama {self.label} no existe (¿ya se borró?)") from exc
                last = exc
                self.log(f"esperando al cómputo de {self.label}… {type(exc).__name__}")
                time.sleep(min(3 + 2 * i, 10))
        raise RuntimeError(f"no conecta a {self.label}: {last}")

    def _branch_gone(self) -> bool:
        try:
            self.w.postgres.get_branch(name=self.branch_path)
            return False
        except Exception as exc:
            return _not_found(exc)

    @property
    def conn(self) -> Any:
        if self._closed_by_user:
            raise RuntimeError(f"la conexión a {self.label} ya se cerró (Limpieza); vuelve a ejecutar desde el principio")
        if self._conn is None or self._conn.closed:
            self._conn = self._open()
        return self._conn

    def _reset(self) -> None:
        """Drop the current connection; closing it makes the server roll back anything it held."""
        old, self._conn = self._conn, None
        if old is not None:
            try:
                old.close()
            except Exception:
                pass

    def _retry(self, fn: Callable[[Any], Any]) -> Any:
        for attempt in (1, 2):
            conn = self.conn
            try:
                return fn(conn)
            except Exception as exc:
                if attempt == 2 or not _lost(exc, conn):
                    raise
                self.log(f"reconectando a {self.label} (el cómputo se había suspendido)…")
                self._reset()

    # -- statements ----------------------------------------------------------------------------
    def q(self, sql: str, params: Any = None) -> list | None:
        def run(conn):
            with conn.cursor() as cur:
                cur.execute(sql, params)
                return cur.fetchall() if cur.description else None
        return self._retry(run)

    def copy_out(self, sql: str) -> str:
        """COPY … TO STDOUT into memory."""
        def run(conn):
            buf = io.StringIO()
            with conn.cursor() as cur:
                cur.copy_expert(sql, buf)
            return buf.getvalue()
        return self._retry(run)

    def transaction(self, fn: Callable[[Any], Any]) -> Any:
        """Run fn(cursor) in one transaction. Rolled back on any error; retried once on a fresh
        connection only if the connection was lost (fn must be idempotent)."""
        def run(conn):
            conn.autocommit = False
            try:
                with conn.cursor() as cur:
                    out = fn(cur)
                conn.commit()
                return out
            except Exception:
                if not conn.closed:
                    conn.rollback()
                raise
            finally:
                if not conn.closed:
                    conn.autocommit = True
        return self._retry(run)

    def close(self) -> None:
        self._reset()
        self._closed_by_user = True
