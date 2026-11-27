"""core/connectors/postgres.py: PostgreSQL provider (the first connector).

Navigation:
  PostgresConfig         validated connection settings (unknown fields rejected)
  translate_params       `:name` placeholders -> psycopg binds, SQL-aware scanner
  PostgresConnector      open / close / run_query / validate
  _translate_error       psycopg exceptions -> typed ConnectorErrors (secret redacted)

Read-only has layers (defense in depth), strongest first:
  1. a read-only DB role (recommended in the connection docs; not enforceable here)
  2. session read-only: `default_transaction_read_only=on` + READ ONLY transactions
  3. every query runs inside a DECLARE ... CURSOR, which accepts one SELECT-like
     statement only, so multi-statement and DDL/DML payloads are rejected outright
"""
import re
import uuid
from typing import Any, Mapping

import psycopg
from pydantic import BaseModel, ConfigDict, Field

from core.connectors.base import Connector, QueryResult
from core.connectors.errors import (AuthenticationFailed, ConnectionFailed, ConnectorError,
                                    QueryRejected, QueryTimeout, ResultTooLarge, redact)
from core.connectors.registry import register

_NAME = re.compile(r"[A-Za-z_]\w*")
_DOLLAR_TAG = re.compile(r"\$[A-Za-z_]*\$")


class PostgresConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")  # invariant: unknown fields rejected

    host: str = Field(min_length=1)
    port: int = Field(default=5432, ge=1, le=65535)
    database: str = Field(min_length=1)
    user: str = Field(min_length=1)
    sslmode: str | None = None


def translate_params(sql: str) -> tuple[str, list[str]]:
    """Convert `:name` placeholders to psycopg's `%(name)s` and return the names used.

    Skips string literals, quoted identifiers, comments, dollar-quoted bodies, and
    `::` casts. Literal `%` is doubled so psycopg doesn't treat it as a placeholder.
    Values are never formatted into the text: they are bound by the driver.
    Known limit: array slices like `a[x:y]` with a name after the colon look like a param.
    """
    out: list[str] = []
    names: list[str] = []
    i, n = 0, len(sql)
    while i < n:
        c = sql[i]
        if c in ("'", '"'):  # string literal or quoted identifier ('' / "" escapes)
            j = i + 1
            while j < n:
                if sql[j] == c:
                    if j + 1 < n and sql[j + 1] == c:
                        j += 2
                        continue
                    break
                j += 1
            out.append(sql[i : j + 1].replace("%", "%%"))
            i = j + 1
        elif sql.startswith("--", i):
            j = sql.find("\n", i)
            j = n if j == -1 else j
            out.append(sql[i:j].replace("%", "%%"))
            i = j
        elif sql.startswith("/*", i):
            j = sql.find("*/", i + 2)
            j = n if j == -1 else j + 2
            out.append(sql[i:j].replace("%", "%%"))
            i = j
        elif c == "$" and (m := _DOLLAR_TAG.match(sql, i)):
            tag = m.group()
            j = sql.find(tag, m.end())
            j = n if j == -1 else j + len(tag)
            out.append(sql[i:j].replace("%", "%%"))
            i = j
        elif c == ":":
            if sql.startswith("::", i):
                out.append("::")
                i += 2
            elif m := _NAME.match(sql, i + 1):
                names.append(m.group())
                out.append(f"%({m.group()})s")
                i = m.end()
            else:
                out.append(":")
                i += 1
        elif c == "%":
            out.append("%%")
            i += 1
        else:
            out.append(c)
            i += 1
    return "".join(out), names


@register("postgres")
class PostgresConnector(Connector):
    config_model = PostgresConfig
    _CONNECT_TIMEOUT_S = 10

    def __init__(self, spec):
        super().__init__(spec)
        self._conn: psycopg.Connection | None = None

    @property
    def is_open(self) -> bool:
        return self._conn is not None and not self._conn.closed

    # --- lifecycle -------------------------------------------------------
    def open(self) -> None:
        if self.is_open:
            return
        cfg: PostgresConfig = self.config  # type: ignore[assignment]
        kwargs: dict[str, Any] = dict(
            host=cfg.host, port=cfg.port, dbname=cfg.database, user=cfg.user,
            password=self.spec.secret.get_secret_value(),  # the only place the secret is read
            connect_timeout=self._CONNECT_TIMEOUT_S,
            application_name="statloom-connector",
            # Layer 2: every session in this connection is read-only and UTC.
            options="-c default_transaction_read_only=on -c timezone=UTC",
        )
        if cfg.sslmode:
            kwargs["sslmode"] = cfg.sslmode
        try:
            conn = psycopg.connect(**kwargs)
            conn.read_only = True  # BEGIN READ ONLY for every transaction
            self._conn = conn
        except psycopg.Error as exc:
            raise self._translate_error(exc) from None

    def close(self) -> None:
        conn, self._conn = self._conn, None
        if conn is None:
            return
        try:
            conn.close()
        except Exception:  # close() must never raise
            pass

    # --- work ------------------------------------------------------------
    def run_query(self, sql, params: Mapping[str, Any] | None = None, *,
                  timeout_s: float = 30.0, max_rows: int = 100_000) -> QueryResult:
        if not self.is_open:
            raise ConnectorError("connector is not open")
        params = params or {}
        driver_sql, names = translate_params(sql)
        missing = sorted({n for n in names if n not in params})
        if missing:
            raise QueryRejected(f"missing query parameters: {missing}")
        bound = {n: params[n] for n in names}
        conn = self._conn
        assert conn is not None
        try:
            # Bounded execution: server-side timeout, scoped to this transaction.
            conn.execute("SELECT set_config('statement_timeout', %s, true)",
                         (str(int(timeout_s * 1000)),))
            # Server-side cursor => DECLARE ... CURSOR: one SELECT-like statement only.
            with conn.cursor(name=f"q_{uuid.uuid4().hex}") as cur:
                cur.execute(driver_sql, bound)
                # Fetch one past the cap so we can tell "exactly max_rows" from "too many".
                rows = cur.fetchmany(max_rows + 1)
                columns = tuple(d.name for d in (cur.description or ()))
            if len(rows) > max_rows:
                raise ResultTooLarge(f"query returned more than {max_rows} rows")
            return QueryResult(columns=columns, rows=tuple(tuple(r) for r in rows))
        except psycopg.Error as exc:
            raise self._translate_error(exc) from None
        finally:
            try:
                conn.rollback()  # end the read-only txn; the next query starts clean
            except psycopg.Error:
                pass

    def validate(self) -> None:
        opened_here = not self.is_open
        self.open()
        try:
            conn = self._conn
            assert conn is not None
            try:
                state = conn.execute("SHOW transaction_read_only").fetchone()
                conn.execute("SELECT 1")
            except psycopg.Error as exc:
                raise self._translate_error(exc) from None
            finally:
                conn.rollback()
            if not state or state[0] != "on":
                raise QueryRejected("session is not read-only")
        finally:
            if opened_here:
                self.close()

    # --- errors ----------------------------------------------------------
    def _translate_error(self, exc: Exception) -> ConnectorError:
        """Map a psycopg error to a typed ConnectorError with the secret redacted."""
        msg = redact(str(exc).strip(), self.spec.secret.get_secret_value())
        if isinstance(exc, psycopg.errors.QueryCanceled):  # check before OperationalError
            return QueryTimeout(msg)
        if isinstance(exc, psycopg.errors.InvalidPassword) or "authentication failed" in msg.lower():
            return AuthenticationFailed(msg)
        if isinstance(exc, psycopg.OperationalError):
            return ConnectionFailed(msg)
        if isinstance(exc, psycopg.Error):
            return QueryRejected(msg)
        return ConnectorError(msg)
