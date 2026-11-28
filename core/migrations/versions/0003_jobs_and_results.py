"""0003: job and result tables, and the database-enforced job rules.

Navigation:
  job_insert_guard         BEFORE INSERT: a job starts queued with attempt_count 0
  job_transition_guard     BEFORE UPDATE: legal status transitions, attempt_count rules, lease rule
  forbid_column_change     (from 0002) attached to job: pinned columns never change
  job_succeeded_has_result / result_job_is_succeeded
                           DEFERRED constraint triggers: result exists <=> job succeeded, at commit
  result                   append-only (forbid_mutation)

Written by hand. The job's CHECK constraints are spelled out in full to match the model.
"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

from core.db.base import attach_forbid_mutation, attach_timestamp_triggers

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None

PINNED = ("metric_version_id", "engine_version_id", "connection_id", "parameters", "engine_params",
          "control_variant", "config_hash", "idempotency_key", "max_attempts")


def upgrade() -> None:
    op.create_table(
        "job",
        sa.Column("id", sa.Uuid(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("status", sa.String(), server_default=sa.text("'queued'"), nullable=False),
        sa.Column("attempt_count", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("max_attempts", sa.Integer(), nullable=False),
        sa.Column("metric_version_id", sa.Uuid(), nullable=False),
        sa.Column("engine_version_id", sa.Uuid(), nullable=False),
        sa.Column("connection_id", sa.Uuid(), nullable=False),
        sa.Column("parameters", postgresql.JSONB(), nullable=False),
        sa.Column("engine_params", postgresql.JSONB(), nullable=False),
        sa.Column("control_variant", sa.String(), nullable=False),
        sa.Column("config_hash", sa.String(), nullable=False),
        sa.Column("idempotency_key", sa.String(), nullable=False),
        sa.Column("claimed_by", sa.String(), nullable=True),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("failure_code", sa.String(), nullable=True),
        sa.Column("failure_message", sa.Text(), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_job"),
        sa.ForeignKeyConstraint(["metric_version_id"], ["metric_version.id"],
                                name="fk_job_metric_version_id_metric_version", ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["engine_version_id"], ["engine_version.id"],
                                name="fk_job_engine_version_id_engine_version", ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["connection_id"], ["connection.id"],
                                name="fk_job_connection_id_connection", ondelete="RESTRICT"),
        sa.UniqueConstraint("idempotency_key", name="uq_job_idempotency_key"),
        sa.CheckConstraint("status IN ('queued', 'running', 'succeeded', 'failed')", name="ck_job_status_valid"),
        sa.CheckConstraint("max_attempts >= 1", name="ck_job_max_attempts_positive"),
        sa.CheckConstraint("attempt_count >= 0 AND attempt_count <= max_attempts", name="ck_job_attempts_within_cap"),
        sa.CheckConstraint("(status = 'running') = (claimed_by IS NOT NULL) AND "
                           "(status = 'running') = (lease_expires_at IS NOT NULL)", name="ck_job_running_iff_claimed"),
        sa.CheckConstraint("(status IN ('succeeded', 'failed')) = (finished_at IS NOT NULL)",
                           name="ck_job_finished_iff_terminal"),
        sa.CheckConstraint("status <> 'failed' OR failure_code IS NOT NULL", name="ck_job_failed_has_code"),
        sa.CheckConstraint("status <> 'succeeded' OR (failure_code IS NULL AND failure_message IS NULL)",
                           name="ck_job_succeeded_has_no_failure"),
        sa.CheckConstraint("(failure_code IS NULL) = (failure_message IS NULL)", name="ck_job_failure_fields_together"),
        sa.CheckConstraint("failure_code IS NULL OR failure_code ~ '^[a-z][a-z0-9_]{0,63}$'",
                           name="ck_job_failure_code_format"),
        sa.CheckConstraint("failure_message IS NULL OR char_length(failure_message) <= 500",
                           name="ck_job_failure_message_length"),
        sa.CheckConstraint("char_length(idempotency_key) BETWEEN 1 AND 255", name="ck_job_idempotency_key_length"),
        sa.CheckConstraint("control_variant <> ''", name="ck_job_control_variant_not_empty"),
        sa.CheckConstraint("char_length(config_hash) = 64", name="ck_job_config_hash_length"),
    )
    op.create_index("ix_job_status", "job", ["status"])
    op.create_table(
        "result",
        sa.Column("id", sa.Uuid(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("job_id", sa.Uuid(), nullable=False),
        sa.Column("output", postgresql.JSONB(), nullable=False),
        sa.Column("contract_version", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_result"),
        sa.ForeignKeyConstraint(["job_id"], ["job.id"], name="fk_result_job_id_job", ondelete="RESTRICT"),
        sa.UniqueConstraint("job_id", name="uq_result_job_id"),
    )

    op.execute("""
        CREATE FUNCTION job_insert_guard() RETURNS trigger AS $$
        BEGIN
            IF NEW.status <> 'queued' OR NEW.attempt_count <> 0 THEN
                RAISE EXCEPTION 'a job must be inserted as queued with attempt_count 0';
            END IF;
            RETURN NEW;
        END $$ LANGUAGE plpgsql
    """)
    op.execute("""
        CREATE FUNCTION job_transition_guard() RETURNS trigger AS $$
        BEGIN
            IF NEW.attempt_count < OLD.attempt_count OR NEW.attempt_count > OLD.attempt_count + 1 THEN
                RAISE EXCEPTION 'attempt_count may stay or rise by exactly one';
            END IF;
            IF OLD.status = 'queued' AND NEW.status = 'running' THEN
                IF NEW.attempt_count <> OLD.attempt_count + 1 THEN
                    RAISE EXCEPTION 'claiming a job must start a new attempt';
                END IF;
            ELSIF OLD.status = 'running' AND NEW.status = 'running' THEN
                IF NEW.attempt_count = OLD.attempt_count + 1 AND OLD.lease_expires_at >= now() THEN
                    RAISE EXCEPTION 'cannot start a new attempt while the lease is still valid';
                END IF;
                IF NEW.claimed_by IS DISTINCT FROM OLD.claimed_by AND NEW.attempt_count = OLD.attempt_count THEN
                    RAISE EXCEPTION 'changing the claimant requires a new attempt';
                END IF;
            ELSIF OLD.status = 'running' AND NEW.status IN ('succeeded', 'failed') THEN
                IF NEW.attempt_count <> OLD.attempt_count THEN
                    RAISE EXCEPTION 'finishing a job cannot change attempt_count';
                END IF;
            ELSIF OLD.status = 'failed' AND NEW.status = 'queued' THEN
                IF OLD.attempt_count >= OLD.max_attempts THEN
                    RAISE EXCEPTION 'retry cap reached';
                END IF;
                IF NEW.attempt_count <> OLD.attempt_count THEN
                    RAISE EXCEPTION 'requeueing cannot change attempt_count';
                END IF;
            ELSE
                RAISE EXCEPTION 'illegal job transition % -> %', OLD.status, NEW.status;
            END IF;
            RETURN NEW;
        END $$ LANGUAGE plpgsql
    """)
    op.execute("""
        CREATE FUNCTION job_succeeded_has_result() RETURNS trigger AS $$
        BEGIN
            IF NEW.status = 'succeeded' AND NOT EXISTS (SELECT 1 FROM result WHERE job_id = NEW.id) THEN
                RAISE EXCEPTION 'job % is succeeded but has no result', NEW.id;
            END IF;
            RETURN NULL;
        END $$ LANGUAGE plpgsql
    """)
    op.execute("""
        CREATE FUNCTION result_job_is_succeeded() RETURNS trigger AS $$
        BEGIN
            IF NOT EXISTS (SELECT 1 FROM job WHERE id = NEW.job_id AND status = 'succeeded') THEN
                RAISE EXCEPTION 'result for job % requires the job to be succeeded', NEW.job_id;
            END IF;
            RETURN NULL;
        END $$ LANGUAGE plpgsql
    """)

    for ddl in attach_timestamp_triggers("job"):
        op.execute(ddl)
    cols = ", ".join(f"'{c}'" for c in PINNED)
    op.execute(f"CREATE TRIGGER job_forbid_pinned_change BEFORE UPDATE ON job "
               f"FOR EACH ROW EXECUTE FUNCTION forbid_column_change({cols})")
    op.execute("CREATE TRIGGER job_insert_guard BEFORE INSERT ON job "
               "FOR EACH ROW EXECUTE FUNCTION job_insert_guard()")
    op.execute("CREATE TRIGGER job_transition_guard BEFORE UPDATE ON job "
               "FOR EACH ROW EXECUTE FUNCTION job_transition_guard()")
    op.execute("CREATE CONSTRAINT TRIGGER job_succeeded_has_result AFTER INSERT OR UPDATE ON job "
               "DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION job_succeeded_has_result()")
    op.execute("CREATE CONSTRAINT TRIGGER result_job_is_succeeded AFTER INSERT ON result "
               "DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION result_job_is_succeeded()")
    op.execute(attach_forbid_mutation("result"))


def downgrade() -> None:
    op.drop_table("result")
    op.drop_table("job")
    for fn in ("result_job_is_succeeded", "job_succeeded_has_result", "job_transition_guard", "job_insert_guard"):
        op.execute(f"DROP FUNCTION {fn}()")
