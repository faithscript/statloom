"""0002: catalog tables (connection, metric, metric_version, engine, engine_version).

Navigation:
  forbid_column_change   trigger function: raise if any listed column changes   -> pinned columns (used in 0003)
  tables                 connection, metric, metric_version, engine, engine_version
  triggers               every table here is append-only (forbid_mutation)

Written by hand (autogenerate does not see triggers). Constraint names are spelled out in
full so the migrated schema equals what the models declare (checked in test_db.py).
"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

from core.db.base import attach_forbid_mutation

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None

_TS = lambda: [  # noqa: E731
    sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
    sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
]
_ID = lambda t: sa.Column("id", sa.Uuid(), server_default=sa.text("gen_random_uuid()"), nullable=False)  # noqa: E731


def upgrade() -> None:
    # Raises if any column named in the trigger's arguments changes. Compared as jsonb so it
    # works for every column type, including jsonb columns.
    op.execute("""
        CREATE FUNCTION forbid_column_change() RETURNS trigger AS $$
        DECLARE col text;
        BEGIN
            FOREACH col IN ARRAY TG_ARGV LOOP
                IF to_jsonb(NEW) -> col IS DISTINCT FROM to_jsonb(OLD) -> col THEN
                    RAISE EXCEPTION 'column % is immutable (table %)', col, TG_TABLE_NAME;
                END IF;
            END LOOP;
            RETURN NEW;
        END $$ LANGUAGE plpgsql
    """)

    op.create_table(
        "connection", _ID("connection"),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("type", sa.String(), nullable=False),
        sa.Column("config", postgresql.JSONB(), nullable=False),
        sa.Column("encrypted_secret", sa.Text(), nullable=False),
        *_TS(),
        sa.PrimaryKeyConstraint("id", name="pk_connection"),
        sa.CheckConstraint("name <> '' AND type <> ''", name="ck_connection_name_and_type_not_empty"),
    )
    op.create_table(
        "metric", _ID("metric"),
        sa.Column("name", sa.String(), nullable=False),
        *_TS(),
        sa.PrimaryKeyConstraint("id", name="pk_metric"),
        sa.UniqueConstraint("name", name="uq_metric_name"),
    )
    op.create_table(
        "metric_version", _ID("metric_version"),
        sa.Column("metric_id", sa.Uuid(), nullable=False),
        sa.Column("version_number", sa.Integer(), nullable=False),
        sa.Column("sql_template", sa.Text(), nullable=False),
        sa.Column("param_spec", postgresql.JSONB(), nullable=False),
        sa.Column("metric_type", sa.String(), nullable=False),
        sa.Column("content_hash", sa.String(), nullable=False),
        *_TS(),
        sa.PrimaryKeyConstraint("id", name="pk_metric_version"),
        sa.ForeignKeyConstraint(["metric_id"], ["metric.id"], name="fk_metric_version_metric_id_metric",
                                ondelete="RESTRICT"),
        sa.UniqueConstraint("metric_id", "version_number", name="uq_metric_version_metric_id_version_number"),
        sa.CheckConstraint("version_number >= 1", name="ck_metric_version_version_number_positive"),
        sa.CheckConstraint("metric_type IN ('continuous', 'proportion')", name="ck_metric_version_metric_type_valid"),
        sa.CheckConstraint("char_length(content_hash) = 64", name="ck_metric_version_content_hash_length"),
    )
    op.create_table(
        "engine", _ID("engine"),
        sa.Column("name", sa.String(), nullable=False),
        *_TS(),
        sa.PrimaryKeyConstraint("id", name="pk_engine"),
        sa.UniqueConstraint("name", name="uq_engine_name"),
    )
    op.create_table(
        "engine_version", _ID("engine_version"),
        sa.Column("engine_id", sa.Uuid(), nullable=False),
        sa.Column("version_number", sa.Integer(), nullable=False),
        sa.Column("engine_name", sa.String(), nullable=False),
        sa.Column("declared_version", sa.String(), nullable=False),
        sa.Column("contract_version", sa.Integer(), nullable=False),
        *_TS(),
        sa.PrimaryKeyConstraint("id", name="pk_engine_version"),
        sa.ForeignKeyConstraint(["engine_id"], ["engine.id"], name="fk_engine_version_engine_id_engine",
                                ondelete="RESTRICT"),
        sa.UniqueConstraint("engine_id", "version_number", name="uq_engine_version_engine_id_version_number"),
        sa.CheckConstraint("version_number >= 1", name="ck_engine_version_version_number_positive"),
        sa.CheckConstraint("engine_name <> '' AND declared_version <> ''",
                           name="ck_engine_version_name_and_version_not_empty"),
    )
    for table in ("connection", "metric", "metric_version", "engine", "engine_version"):
        op.execute(attach_forbid_mutation(table))


def downgrade() -> None:
    for table in ("engine_version", "engine", "metric_version", "metric", "connection"):
        op.drop_table(table)
    op.execute("DROP FUNCTION forbid_column_change()")
