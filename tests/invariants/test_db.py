"""Invariant tests for core/db, grouped by the invariant they prove.

Navigation: UTC | atomic transactions | row locking | schema currency |
            timestamp triggers | forbid_mutation | migrations match models
Widget is a scratch table (own declarative base) used to exercise the triggers.
"""
import threading
import time
from datetime import datetime, timedelta, timezone

import pytest
from alembic.autogenerate import compare_metadata
from alembic.runtime.migration import MigrationContext
from sqlalchemy import String, create_engine, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from core.db.base import (Base, TimestampMixin, attach_forbid_mutation,
                          attach_timestamp_triggers)
from core.db.session import (SchemaMismatchError, check_schema_current, get_engine,
                             get_session_factory, lock_row, session_scope)


class ScratchBase(DeclarativeBase):  # keeps test tables out of the real Base.metadata
    pass


class Widget(TimestampMixin, ScratchBase):
    __tablename__ = "widget"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String)


@pytest.fixture
def widget_db(db_url):
    engine = get_engine(db_url)
    ScratchBase.metadata.create_all(engine)
    with engine.begin() as c:
        for stmt in attach_timestamp_triggers("widget"):
            c.execute(text(stmt))
    return db_url


@pytest.fixture
def factory(widget_db):
    return get_session_factory(get_engine(widget_db))


def _add(factory, name="a") -> int:
    with session_scope(factory) as s:
        w = Widget(name=name)
        s.add(w)
        s.flush()
        return w.id


# --- UTC ---------------------------------------------------------------
def test_session_timezone_is_utc(widget_db):
    with get_engine(widget_db).connect() as c:
        assert c.execute(text("SHOW timezone")).scalar() == "UTC"


def test_timestamps_are_tz_aware_utc(factory):
    wid = _add(factory)
    with session_scope(factory) as s:
        w = s.get(Widget, wid)
        assert w.created_at.utcoffset() == timedelta(0)
        assert w.updated_at.utcoffset() == timedelta(0)


# --- atomic transactions ----------------------------------------------
def test_session_scope_rolls_back_all_writes_on_error(factory):
    with pytest.raises(RuntimeError):
        with session_scope(factory) as s:
            s.add(Widget(name="first"))
            s.flush()
            raise RuntimeError("boom")
    with session_scope(factory) as s:
        assert s.query(Widget).count() == 0


def test_session_scope_commits_all_writes_on_success(factory):
    with session_scope(factory) as s:
        s.add_all([Widget(name="a"), Widget(name="b")])
    with session_scope(factory) as s:
        assert s.query(Widget).count() == 2


def test_autoflush_disabled(factory):
    with session_scope(factory) as s:
        s.add(Widget(name="pending"))
        assert s.query(Widget).count() == 0  # not flushed yet


# --- row locking ------------------------------------------------------
def test_lock_row_serializes_concurrent_holders(factory):
    wid = _add(factory)
    events: list[str] = []
    holding = threading.Event()

    def first():
        with session_scope(factory) as s:
            lock_row(s, Widget, wid)
            events.append("first-locked")
            holding.set()
            time.sleep(0.5)
            events.append("first-releasing")

    def second():
        holding.wait()
        with session_scope(factory) as s:
            lock_row(s, Widget, wid)
            events.append("second-locked")

    ts = [threading.Thread(target=first), threading.Thread(target=second)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert events == ["first-locked", "first-releasing", "second-locked"]


def test_lock_row_missing_raises(factory):
    with pytest.raises(LookupError):
        with session_scope(factory) as s:
            lock_row(s, Widget, 999)


# --- schema currency --------------------------------------------------
def test_check_schema_current_passes_at_head(db_url):
    check_schema_current(get_engine(db_url))


def test_check_schema_current_fails_when_db_behind(fresh_db_url):
    with pytest.raises(SchemaMismatchError):
        check_schema_current(get_engine(fresh_db_url))


def test_check_schema_current_fails_when_db_ahead(db_url):
    engine = get_engine(db_url)
    with engine.begin() as c:
        c.execute(text("UPDATE alembic_version SET version_num = 'ffff'"))
    with pytest.raises(SchemaMismatchError):
        check_schema_current(engine)


# --- timestamp triggers -----------------------------------------------
def test_update_bumps_updated_at_and_never_decreases(factory):
    wid = _add(factory)
    with session_scope(factory) as s:
        before = s.get(Widget, wid).updated_at
    time.sleep(0.05)
    with session_scope(factory) as s:
        w = s.get(Widget, wid)
        w.name = "changed"
    with session_scope(factory) as s:
        after = s.get(Widget, wid).updated_at
    assert after > before

    # A client trying to push updated_at backward is clamped.
    with session_scope(factory) as s:
        w = s.get(Widget, wid)
        w.updated_at = datetime(2000, 1, 1, tzinfo=timezone.utc)
    with session_scope(factory) as s:
        assert s.get(Widget, wid).updated_at >= after


def test_created_at_is_immutable(factory):
    wid = _add(factory)
    with pytest.raises(DBAPIError, match="created_at is immutable"):
        with session_scope(factory) as s:
            s.get(Widget, wid).created_at = datetime(2000, 1, 1, tzinfo=timezone.utc)


# --- forbid_mutation --------------------------------------------------
def test_forbid_mutation_blocks_update_and_delete(widget_db):
    engine = get_engine(widget_db)
    with engine.begin() as c:
        c.execute(text("CREATE TABLE frozen (id int primary key, v text)"))
        c.execute(text(attach_forbid_mutation("frozen")))
        c.execute(text("INSERT INTO frozen VALUES (1, 'x')"))  # insert still allowed
    for stmt in ("UPDATE frozen SET v='y'", "DELETE FROM frozen"):
        with pytest.raises(DBAPIError, match="forbidden on immutable table"):
            with engine.begin() as c:
                c.execute(text(stmt))


# --- migrations match models -----------------------------------------
def test_migrated_schema_matches_models(db_url):
    import core.models  # noqa: F401  (registers every table on Base.metadata)
    engine = create_engine(db_url)
    with engine.connect() as conn:
        diff = compare_metadata(MigrationContext.configure(conn), Base.metadata)
    assert diff == []
