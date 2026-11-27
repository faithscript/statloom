"""Shared test fixtures (real Postgres, never SQLite: triggers and FOR UPDATE are Postgres behavior).

Navigation:
  pg_admin_url   session-wide Postgres: $TEST_PG_URL, else a throwaway local cluster
  fresh_db_url   a brand-new empty database per test (no migrations applied)
  db_url         same, migrated to head
  alembic_cfg    helper to build an Alembic Config pointed at a given URL
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
    subprocess.run(["pg_ctl", "-D", data, "-o", f"-p {port} -k {data}", "-w", "start",
                    "-l", f"{data}/log"], check=True, capture_output=True)
    try:
        yield f"postgresql+psycopg://postgres@127.0.0.1:{port}/postgres"
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
