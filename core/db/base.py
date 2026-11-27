"""core/db/base.py: declarative base and timestamp support for every table.

Navigation:
  NAMING_CONVENTION        stable constraint/index names, so migrations are deterministic
  Base                     parent class for all ORM models (its metadata drives Alembic)
  TimestampMixin           created_at / updated_at columns (DB-set, UTC)
  attach_timestamp_triggers  DDL: enforce updated_at + created_at rules on a table
  attach_forbid_mutation     DDL: make a table append-only (used by version tables)

The triggers themselves are defined in migrations/versions/0001_trigger_functions.py;
the helpers here only attach them to specific tables.
"""
from datetime import datetime

from sqlalchemy import DateTime, MetaData, func
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

# Invariant: migrated schema == model schema. Explicit names mean autogenerate
# never sees phantom differences from database-generated constraint names.
NAMING_CONVENTION = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    """Parent of every ORM model. Alembic compares the DB against Base.metadata."""

    metadata = MetaData(naming_convention=NAMING_CONVENTION)


class TimestampMixin:
    """Adds created_at / updated_at to a model.

    Invariant: timestamps are timezone-aware, UTC, and set by the database
    (server_default=now()), never by application code. The triggers attached via
    attach_timestamp_triggers() keep them honest on UPDATE.
    """

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


def attach_timestamp_triggers(table: str) -> list[str]:
    """Return DDL that attaches the two timestamp triggers to `table`.

    Enforces: updated_at is bumped on UPDATE and never decreases;
    created_at can never be changed. Call from a migration after creating the table.
    """
    return [
        f"CREATE TRIGGER {table}_touch_updated_at BEFORE UPDATE ON {table} "
        "FOR EACH ROW EXECUTE FUNCTION touch_updated_at()",
        f"CREATE TRIGGER {table}_guard_created_at BEFORE UPDATE ON {table} "
        "FOR EACH ROW EXECUTE FUNCTION guard_created_at()",
    ]


def attach_forbid_mutation(table: str) -> str:
    """Return DDL making `table` append-only: any UPDATE or DELETE raises.

    Enforces: published metric/engine versions are immutable.
    """
    return (
        f"CREATE TRIGGER {table}_forbid_mutation BEFORE UPDATE OR DELETE ON {table} "
        "FOR EACH ROW EXECUTE FUNCTION forbid_mutation()"
    )
