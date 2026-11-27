"""Invariant tests for core/connectors, grouped by the invariant they prove.

Navigation: read-only | multi-statement | credentials | bound parameters | timeout |
            row cap | no leaks | error classes | registry | config | contract suite
            (pluggable + uniform) | independence
Postgres tests run against a real database standing in as the user's database.
"""
import ast
import logging
import socket
import time
from pathlib import Path

import psycopg
import pytest
from sqlalchemy import text

from core.connectors import (AuthenticationFailed, ConfigInvalid, ConnectionFailed,
                             ConnectionSpec, QueryRejected, QueryResult, QueryTimeout,
                             ResultTooLarge, UnknownConnectorType, create_connector,
                             register, registered_types)
from core.connectors.registry import unregister
from tests.conftest import APP_USER_PASSWORD
from tests.fakes import FakeConnector


def pg_spec(db, *, user=None, secret=None, **overrides):
    config = {"host": db["host"], "port": db["port"], "database": db["database"],
              "user": user or db["user"], **overrides}
    return ConnectionSpec(type="postgres", config=config, secret=secret or db["password"])


@pytest.fixture
def connector(user_db):
    with create_connector(pg_spec(user_db)) as c:
        yield c


def table_rows(db) -> int:
    with db["engine"].connect() as c:
        return c.execute(text("SELECT count(*) FROM t")).scalar()


# --- read-only --------------------------------------------------------
@pytest.mark.parametrize("sql", [
    "INSERT INTO t VALUES (99, 'x')",
    "UPDATE t SET name = 'x'",
    "DELETE FROM t",
    "CREATE TABLE evil (id int)",
    "DROP TABLE t",
    "SET default_transaction_read_only = off",
    "SET TRANSACTION READ WRITE",
])
def test_writes_and_session_changes_are_rejected(connector, user_db, sql):
    with pytest.raises(QueryRejected):
        connector.run_query(sql)
    assert table_rows(user_db) == 3  # nothing changed


def test_session_layer_blocks_writes_even_if_sql_filter_were_bypassed(connector, user_db):
    """White-box: layer 2 (read-only session) holds on its own, below the cursor guard."""
    conn = connector._conn
    conn.execute("SET default_transaction_read_only = off")  # session var can be flipped...
    with pytest.raises(psycopg.errors.ReadOnlySqlTransaction):
        conn.execute("INSERT INTO t VALUES (99, 'x')")       # ...but the txn is READ ONLY
    conn.rollback()
    assert table_rows(user_db) == 3


def test_validate_confirms_read_only(user_db):
    create_connector(pg_spec(user_db)).validate()  # returns None, raises on failure


# --- multi-statement --------------------------------------------------
def test_multi_statement_payload_is_rejected(connector, user_db):
    payload = "SELECT 1; SET default_transaction_read_only = off; COMMIT; DROP TABLE t"
    with pytest.raises(QueryRejected):
        connector.run_query(payload)
    assert table_rows(user_db) == 3  # t still exists with its rows


# --- credentials ------------------------------------------------------
def test_secret_is_masked_in_repr_and_str(user_db):
    spec = pg_spec(user_db, secret="s3cret-value")
    assert "s3cret-value" not in repr(spec)
    assert "s3cret-value" not in str(spec)
    assert "s3cret-value" not in repr(create_connector(spec).spec)


def test_wrong_password_is_typed_and_leaks_nothing(user_db, pg_is_local, caplog):
    if not pg_is_local:
        pytest.skip("needs the local cluster, where app_user requires a password")
    bad = "wrong-password-123"
    caplog.set_level(logging.DEBUG)
    with pytest.raises(AuthenticationFailed) as err:
        create_connector(pg_spec(user_db, user="app_user", secret=bad)).open()
    assert not err.value.transient
    assert bad not in str(err.value) and bad not in caplog.text


def test_correct_password_connects(user_db, pg_is_local):
    if not pg_is_local:
        pytest.skip("needs the local cluster, where app_user requires a password")
    spec = pg_spec(user_db, user="app_user", secret=APP_USER_PASSWORD)
    with create_connector(spec) as c:
        assert c.run_query("SELECT count(*) AS n FROM t").rows == ((3,),)


# --- bound parameters -------------------------------------------------
def test_injection_payload_is_matched_literally(connector, user_db):
    payload = "'; DROP TABLE t; --"
    res = connector.run_query("SELECT id FROM t WHERE name = :v", {"v": payload})
    assert res.rows == ((3,),)  # matched the row whose name IS the payload
    assert table_rows(user_db) == 3


def test_casts_colons_in_strings_and_percent_still_work(connector):
    res = connector.run_query(
        "SELECT :n::int AS n, 'a:b' AS s, 'x%' LIKE 'x%' AS pct", {"n": "7"})
    assert res.rows == ((7, "a:b", True),)


def test_missing_parameter_is_rejected(connector):
    with pytest.raises(QueryRejected, match="missing"):
        connector.run_query("SELECT :nope")


# --- bounded execution ------------------------------------------------
def test_timeout_raises_transient_query_timeout(connector):
    start = time.monotonic()
    with pytest.raises(QueryTimeout) as err:
        connector.run_query("SELECT pg_sleep(5)", timeout_s=1)
    assert err.value.transient
    assert time.monotonic() - start < 4
    assert connector.run_query("SELECT 1 AS ok").rows == ((1,),)  # connector still usable


# --- no silent partial data -------------------------------------------
def test_result_over_cap_raises_and_never_truncates(connector):
    with pytest.raises(ResultTooLarge):
        connector.run_query("SELECT g FROM generate_series(1, 1000) g", max_rows=10)


def test_result_exactly_at_cap_is_returned_whole(connector):
    res = connector.run_query("SELECT g FROM generate_series(1, 10) g", max_rows=10)
    assert res.row_count == 10


# --- no leaked connections --------------------------------------------
def _sessions(db) -> int:
    for _ in range(20):  # backends exit asynchronously; poll briefly
        with db["engine"].connect() as c:
            n = c.execute(text(
                "SELECT count(*) FROM pg_stat_activity "
                "WHERE application_name = 'statloom-connector' AND datname = :d"),
                {"d": db["database"]}).scalar()
        if n == 0:
            return 0
        time.sleep(0.1)
    return n


def test_connections_are_closed_after_success_error_and_timeout(user_db):
    with create_connector(pg_spec(user_db)) as c:
        c.run_query("SELECT 1 AS x")
    with pytest.raises(QueryRejected):
        with create_connector(pg_spec(user_db)) as c:
            c.run_query("DROP TABLE t")
    with pytest.raises(QueryTimeout):
        with create_connector(pg_spec(user_db)) as c:
            c.run_query("SELECT pg_sleep(5)", timeout_s=1)
    assert _sessions(user_db) == 0


def test_close_is_idempotent(user_db):
    c = create_connector(pg_spec(user_db))
    c.open()
    c.close()
    c.close()
    assert not c.is_open


# --- error classes ----------------------------------------------------
def test_unreachable_host_is_transient_connection_failed(user_db):
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        dead_port = s.getsockname()[1]  # nothing listens here once the socket closes
    with pytest.raises(ConnectionFailed) as err:
        create_connector(pg_spec(user_db, port=dead_port)).open()
    assert err.value.transient


def test_unknown_column_is_permanent_query_rejected(connector):
    with pytest.raises(QueryRejected) as err:
        connector.run_query("SELECT nope FROM t")
    assert not err.value.transient


# --- registry ---------------------------------------------------------
def test_unknown_type_raises_typed_error():
    with pytest.raises(UnknownConnectorType):
        create_connector(ConnectionSpec(type="oracle", config={}, secret="x"))


def test_duplicate_registration_is_rejected():
    with pytest.raises(ConfigInvalid):
        register("postgres")(FakeConnector)


# --- config -----------------------------------------------------------
@pytest.mark.parametrize("config", [
    {"database": "d", "user": "u"},                               # missing host
    {"host": "h", "database": "d", "user": "u", "bogus": 1},      # unknown field
    {"host": "h", "database": "d", "user": "u", "port": 70000},   # out of range
])
def test_invalid_config_is_rejected_before_connecting(config):
    with pytest.raises(ConfigInvalid):
        create_connector(ConnectionSpec(type="postgres", config=config, secret="x"))


def test_config_error_message_never_echoes_input_values():
    spec = ConnectionSpec(type="postgres", secret="x",
                          config={"host": "h", "database": "d", "user": "u",
                                  "port": "my-private-value"})
    with pytest.raises(ConfigInvalid) as err:
        create_connector(spec)
    assert "my-private-value" not in str(err.value)


# --- contract suite: pluggable + uniform ------------------------------
@pytest.fixture
def fake_registered():
    register("fake")(FakeConnector)  # no core edits: just register at runtime
    yield
    unregister("fake")


@pytest.fixture(params=["fake", "postgres"])
def provider_connector(request, user_db, fake_registered):
    if request.param == "fake":
        return create_connector(ConnectionSpec(type="fake", config={}, secret="x"))
    return create_connector(pg_spec(user_db))


N_ROWS = "SELECT g AS n FROM generate_series(1, :n) g"  # the fake understands :n too


def test_new_provider_registers_without_core_changes(fake_registered):
    assert "fake" in registered_types()
    assert isinstance(create_connector(ConnectionSpec(type="fake", config={}, secret="x")),
                      FakeConnector)


def test_every_provider_returns_the_same_result_shape(provider_connector):
    with provider_connector as c:
        res = c.run_query(N_ROWS, {"n": 3})
    assert isinstance(res, QueryResult)
    assert res.columns == ("n",)
    assert res.rows == ((1,), (2,), (3,))
    assert res.row_count == 3


def test_every_provider_enforces_the_row_cap(provider_connector):
    with provider_connector as c, pytest.raises(ResultTooLarge):
        c.run_query(N_ROWS, {"n": 50}, max_rows=10)


def test_every_provider_closes_cleanly_and_idempotently(provider_connector):
    with provider_connector as c:
        assert c.is_open
    assert not provider_connector.is_open
    provider_connector.close()  # second close is a no-op
    assert not provider_connector.is_open


def test_every_provider_validates(provider_connector):
    provider_connector.validate()


# --- independence -----------------------------------------------------
def test_connectors_import_nothing_from_statloom_persistence():
    banned = ("core.db", "core.models", "core.services", "core.schemas", "core.security")
    pkg = Path(__file__).resolve().parents[2] / "core" / "connectors"
    for path in pkg.glob("*.py"):
        for node in ast.walk(ast.parse(path.read_text())):
            names = []
            if isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                names = [node.module]
            for name in names:
                assert not name.startswith(banned), f"{path.name} imports {name}"
