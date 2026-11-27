"""0001: database-level trigger functions (timestamp upkeep, append-only tables).

Navigation:
  touch_updated_at   BEFORE UPDATE: sets updated_at, never backward   -> monotonic updated_at
  guard_created_at   BEFORE UPDATE: rejects created_at changes        -> immutable created_at
  forbid_mutation    BEFORE UPDATE/DELETE: always raises              -> immutable version rows

These are only the functions. Tables opt in by attaching them with the helpers
in core/db/base.py (attach_timestamp_triggers, attach_forbid_mutation), so the
guarantees hold even if application code is bypassed.
"""
from alembic import op

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    # updated_at can only move forward. now() is the transaction start time,
    # so two updates in one transaction share a timestamp (non-decreasing).
    op.execute("""
        CREATE FUNCTION touch_updated_at() RETURNS trigger AS $$
        BEGIN
            NEW.updated_at = GREATEST(OLD.updated_at, now());
            RETURN NEW;
        END $$ LANGUAGE plpgsql
    """)
    # created_at is write-once.
    op.execute("""
        CREATE FUNCTION guard_created_at() RETURNS trigger AS $$
        BEGIN
            IF NEW.created_at IS DISTINCT FROM OLD.created_at THEN
                RAISE EXCEPTION 'created_at is immutable (table %)', TG_TABLE_NAME;
            END IF;
            RETURN NEW;
        END $$ LANGUAGE plpgsql
    """)
    # Append-only tables: any UPDATE or DELETE is an error.
    op.execute("""
        CREATE FUNCTION forbid_mutation() RETURNS trigger AS $$
        BEGIN
            RAISE EXCEPTION '% is forbidden on immutable table %', TG_OP, TG_TABLE_NAME;
        END $$ LANGUAGE plpgsql
    """)


def downgrade() -> None:
    op.execute("DROP FUNCTION forbid_mutation()")
    op.execute("DROP FUNCTION guard_created_at()")
    op.execute("DROP FUNCTION touch_updated_at()")
