"""Shared test fixtures (real Postgres, never SQLite: triggers and FOR UPDATE are Postgres behavior).

Navigation:
  pg_admin_url   session-wide Postgres: $TEST_PG_URL, else a throwaway local cluster
  fresh_db_url   a brand-new empty database per test (no migrations applied)
  db_url         same, migrated to head
  alembic_cfg    helper to build an Alembic Config pointed at a given URL
  user_db        a seeded "user database" for connector tests (table t, role app_user)
  pg_is_local    True when the throwaway cluster is in use (password-auth tests need it)
"""
import os
import shutil
import socket
import subprocess
import tempfile
import uuid

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, text


APP_USER_PASSWORD = "correct-horse-battery"


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="session")
def pg_admin_url():
    if os.environ.get("TEST_PG_URL"):
        yield os.environ["TEST_PG_URL"]
        return
    if not shutil.which("initdb"):
        pytest.skip("no Postgres available (set TEST_PG_URL or install initdb)")
    data = tempfile.mkdtemp(prefix="statloom_pg_")
    port = _free_port()
    subprocess.run(["initdb", "-D", data, "-U", "postgres", "-A", "trust"],
                   check=True, capture_output=True)
    # `app_user` must present a password over TCP (everyone else stays trust), so
    # connector tests can prove authentication failures are detected and redacted.
    hba = f"{data}/pg_hba.conf"
    with open(hba) as f:
        rules = f.read()
    with open(hba, "w") as f:
        f.write("host all app_user 127.0.0.1/32 scram-sha-256\n" + rules)
    subprocess.run(["pg_ctl", "-D", data, "-o", f"-p {port} -k {data}", "-w", "start",
                    "-l", f"{data}/log"], check=True, capture_output=True)
    try:
        url = f"postgresql+psycopg://postgres@127.0.0.1:{port}/postgres"
        admin = create_engine(url, isolation_level="AUTOCOMMIT")
        with admin.connect() as c:
            c.execute(text(f"CREATE ROLE app_user LOGIN PASSWORD '{APP_USER_PASSWORD}'"))
        admin.dispose()
        yield url
    finally:
        subprocess.run(["pg_ctl", "-D", data, "-m", "immediate", "stop"], capture_output=True)
        shutil.rmtree(data, ignore_errors=True)


def _alembic_cfg(url: str) -> Config:
    cfg = Config("alembic.ini")
    cfg.set_main_option("sqlalchemy.url", url)
    return cfg


@pytest.fixture
def fresh_db_url(pg_admin_url):
    """An empty database (no migrations applied)."""
    name = f"t_{uuid.uuid4().hex[:12]}"
    admin = create_engine(pg_admin_url, isolation_level="AUTOCOMMIT")
    with admin.connect() as c:
        c.execute(text(f"CREATE DATABASE {name}"))
    url = pg_admin_url.rsplit("/", 1)[0] + f"/{name}"
    yield url
    with admin.connect() as c:
        c.execute(text(f"DROP DATABASE {name} WITH (FORCE)"))
    admin.dispose()


@pytest.fixture
def db_url(fresh_db_url):
    """A database migrated to head."""
    command.upgrade(_alembic_cfg(fresh_db_url), "head")
    return fresh_db_url


@pytest.fixture
def alembic_cfg():
    return _alembic_cfg


@pytest.fixture(scope="session")
def pg_is_local() -> bool:
    return not os.environ.get("TEST_PG_URL")


@pytest.fixture
def user_db(fresh_db_url, pg_is_local):
    """A seeded stand-in for a user's database. `user`/`password` connect as the admin
    (a role that CAN write, so read-only tests prove the connector, not the role)."""
    from sqlalchemy.engine import make_url

    u = make_url(fresh_db_url)
    engine = create_engine(fresh_db_url, isolation_level="AUTOCOMMIT")
    with engine.connect() as c:
        c.execute(text("CREATE TABLE t (id int PRIMARY KEY, name text)"))
        # Row 3's name is a SQL-injection payload, used to prove values are bound.
        c.execute(text("INSERT INTO t VALUES (1, 'alice'), (2, 'bob'), "
                       "(3, '''; DROP TABLE t; --')"))
        if pg_is_local:
            c.execute(text("GRANT ALL ON t TO app_user"))
    yield {"host": u.host, "port": u.port, "database": u.database,
           "user": u.username, "password": u.password or "unused", "engine": engine}
    engine.dispose()
