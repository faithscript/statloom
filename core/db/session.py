"""core/db/session.py: engine, sessions, and the one transaction boundary.

Navigation:
  SchemaMismatchError     raised when the DB migration state != code
  get_engine              one cached engine per URL, connections forced to UTC
  get_session_factory     session settings (autoflush / expire_on_commit off)
  session_scope           THE way to run DB work: commit / rollback / close
  lock_row                SELECT ... FOR UPDATE to serialize concurrent writers
  check_schema_current    startup guard: DB revision must equal code head

Rule for the rest of the codebase: services never create sessions themselves;
they receive one from session_scope.
"""
import os
from contextlib import contextmanager
from functools import lru_cache
from pathlib import Path
from typing import Iterator, Type, TypeVar

from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import Engine, create_engine
from sqlalchemy.orm import Session, sessionmaker

T = TypeVar("T")
_MIGRATIONS_DIR = Path(__file__).resolve().parent.parent / "migrations"


class SchemaMismatchError(RuntimeError):
    """The database's Alembic revision differs from the code's head revision."""


# --- Engine ------------------------------------------------------------
@lru_cache(maxsize=None)
def get_engine(url: str | None = None) -> Engine:
    """Return the shared engine for `url` (default: $DATABASE_URL).

    Invariant: all timestamps are UTC. Every connection sets timezone=UTC,
    so now() and timestamptz reads never depend on server or client locale.
    """
    url = url or os.environ["DATABASE_URL"]
    return create_engine(
        url,
        connect_args={"options": "-c timezone=UTC"},
        pool_pre_ping=True,  # drop dead connections instead of failing a job
    )


# --- Sessions and transactions ----------------------------------------
def get_session_factory(engine: Engine) -> sessionmaker[Session]:
    """Build the session factory.

    autoflush=False: pending changes are sent only on explicit flush/commit,
    so no hidden half-written state.
    expire_on_commit=False: objects stay readable after the session closes
    (convenience only, not tied to an invariant).
    """
    return sessionmaker(engine, expire_on_commit=False, autoflush=False)


@contextmanager
def session_scope(factory: sessionmaker[Session] | None = None) -> Iterator[Session]:
    """Run a block as ONE transaction: commit on success, roll back on any error.

    Invariant: multi-write operations are atomic. For example, inserting a
    result and marking its job succeeded either both happen or neither does.
    """
    factory = factory or get_session_factory(get_engine())
    session = factory()
    try:
        yield session
        session.commit()
    except BaseException:
        session.rollback()
        raise
    finally:
        session.close()


# --- Concurrency -------------------------------------------------------
def lock_row(session: Session, model: Type[T], id_) -> T:
    """Fetch a row with SELECT ... FOR UPDATE; other lockers block until we finish.

    Invariants: version numbers are gapless under concurrent registration, and
    only one worker can claim a job. Must be called inside session_scope.
    """
    row = session.get(model, id_, with_for_update=True)
    if row is None:
        raise LookupError(f"{model.__name__} {id_!r} not found")
    return row


# --- Startup guard -----------------------------------------------------
def check_schema_current(engine: Engine) -> None:
    """Raise SchemaMismatchError unless the DB is exactly at the code's head revision.

    Invariant: schema changes come only through migrations. Catches a DB that
    is behind (migration not run) or ahead (code is older than the DB).
    """
    cfg = Config()
    cfg.set_main_option("script_location", str(_MIGRATIONS_DIR))
    code_heads = set(ScriptDirectory.from_config(cfg).get_heads())
    with engine.connect() as conn:
        db_heads = set(MigrationContext.configure(conn).get_current_heads())
    if db_heads != code_heads:
        raise SchemaMismatchError(f"DB at {sorted(db_heads)}, code expects {sorted(code_heads)}")
