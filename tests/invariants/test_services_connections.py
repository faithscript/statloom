"""Invariant tests for core/services/connections.py. Real PostgreSQL (the Statloom database, plus a
seeded stand-in user database to prove a loaded spec really connects).

Navigation: stored encrypted, returned without secret, validated before storage, loads a working
spec, masked, wrong key, no plaintext anywhere.
"""
import logging
import uuid

import pytest
from cryptography.fernet import Fernet
from pydantic import SecretStr
from sqlalchemy import text

from core.connectors import ConfigInvalid, UnknownConnectorType, create_connector
from core.db.session import get_engine, get_session_factory, session_scope
from core.security.secrets import ENV_VAR, SecretDecryptionError
from core.services.connections import create_connection, get_connection, load_connection_spec
from core.services.errors import NotFound

SECRET = "distinctive-db-password-4471"


@pytest.fixture(autouse=True)
def secret_key(monkeypatch):
    monkeypatch.setenv(ENV_VAR, Fernet.generate_key().decode())


@pytest.fixture
def factory(db_url):
    return get_session_factory(get_engine(db_url))


def pg_config(user_db):
    return {"host": user_db["host"], "port": user_db["port"], "database": user_db["database"],
            "user": user_db["user"]}


def test_secret_is_stored_only_encrypted_and_appears_nowhere_in_the_row(factory, db_url, user_db):
    with session_scope(factory) as s:
        info = create_connection(s, "prod", "postgres", pg_config(user_db), SecretStr(SECRET))
    with get_engine(db_url).connect() as c:
        row_text = c.execute(text("SELECT row_to_json(connection)::text FROM connection")).scalar()
        cols = [r[0] for r in c.execute(text(
            "SELECT column_name FROM information_schema.columns WHERE table_name='connection'"))]
    assert SECRET not in row_text and "encrypted_secret" in cols
    assert not {"secret", "password", "plaintext_secret"} & set(cols)       # no plaintext column exists
    assert info.name == "prod"


def test_get_connection_has_no_secret_in_any_field_or_repr(factory, user_db):
    with session_scope(factory) as s:
        cid = create_connection(s, "prod", "postgres", pg_config(user_db), SecretStr(SECRET)).id
    with session_scope(factory) as s:
        info = get_connection(s, cid)
    assert SECRET not in repr(info) and not hasattr(info, "secret") and not hasattr(info, "encrypted_secret")
    with pytest.raises(NotFound):
        with session_scope(factory) as s:
            get_connection(s, uuid.uuid4())


@pytest.mark.parametrize("type_,config,exc", [
    ("postgres", {"host": "h"}, ConfigInvalid),                      # missing required fields
    ("postgres", {"host": "h", "database": "d", "user": "u", "password": "x"}, ConfigInvalid),  # unknown field
    ("mysql", {}, UnknownConnectorType),
])
def test_invalid_config_is_rejected_before_anything_is_stored(factory, db_url, type_, config, exc):
    with pytest.raises(exc) as e:
        with session_scope(factory) as s:
            create_connection(s, "bad", type_, config, SecretStr(SECRET))
    assert SECRET not in str(e.value)
    with get_engine(db_url).connect() as c:
        assert c.execute(text("SELECT count(*) FROM connection")).scalar() == 0


def test_creating_a_connection_never_connects(factory):
    unreachable = {"host": "203.0.113.1", "port": 1, "database": "d", "user": "u"}   # TEST-NET, never reachable
    with session_scope(factory) as s:
        create_connection(s, "offline", "postgres", unreachable, SecretStr(SECRET))   # returns immediately


def test_loaded_spec_really_connects_with_the_decrypted_secret(factory, user_db):
    with session_scope(factory) as s:
        cid = create_connection(s, "prod", "postgres", pg_config(user_db), SecretStr(user_db["password"])).id
    with session_scope(factory) as s:
        spec = load_connection_spec(s, cid)
    assert spec.secret.get_secret_value() == user_db["password"]
    with create_connector(spec) as conn:
        assert conn.run_query("SELECT count(*) FROM t", {}).rows[0][0] == 3


def test_loaded_secret_is_masked_in_repr_str_and_logs(factory, user_db, caplog):
    with session_scope(factory) as s:
        cid = create_connection(s, "prod", "postgres", pg_config(user_db), SecretStr(SECRET)).id
    with session_scope(factory) as s:
        spec = load_connection_spec(s, cid)
    with caplog.at_level(logging.DEBUG):
        logging.getLogger("x").info("%s %r", spec, spec)
    assert SECRET not in repr(spec) and SECRET not in str(spec) and SECRET not in caplog.text


def test_a_wrong_key_fails_to_load_without_leaking(factory, user_db, monkeypatch):
    with session_scope(factory) as s:
        cid = create_connection(s, "prod", "postgres", pg_config(user_db), SecretStr(SECRET)).id
    monkeypatch.setenv(ENV_VAR, Fernet.generate_key().decode())
    with pytest.raises(SecretDecryptionError) as e:
        with session_scope(factory) as s:
            load_connection_spec(s, cid)
    assert SECRET not in str(e.value)
    with pytest.raises(NotFound):
        with session_scope(factory) as s:
            load_connection_spec(s, uuid.uuid4())


def test_rotation_by_adding_a_key_keeps_old_connections_loadable(factory, user_db, monkeypatch):
    old = Fernet.generate_key().decode()
    monkeypatch.setenv(ENV_VAR, old)
    with session_scope(factory) as s:
        cid = create_connection(s, "prod", "postgres", pg_config(user_db), SecretStr(SECRET)).id
    monkeypatch.setenv(ENV_VAR, f"{Fernet.generate_key().decode()},{old}")
    with session_scope(factory) as s:
        assert load_connection_spec(s, cid).secret.get_secret_value() == SECRET
