"""core/models/job.py: an analysis job: what was asked, pinned, plus its execution state.

Navigation:
  Job   pinned ids and parameters, status, attempt_count (also the fencing token), lease, failure info

Everything below is enforced by the database (constraints here, triggers in migration 0003), so it
holds no matter which code writes:
  - Pinned: metric_version_id, engine_version_id, connection_id, parameters, engine_params,
    control_variant, config_hash, idempotency_key and max_attempts never change after insert.
  - Inserted as `queued` with attempt_count 0.
  - Legal transitions only: queued->running, running->running (heartbeat / reclaim),
    running->succeeded|failed, failed->queued while attempt_count < max_attempts. succeeded is
    terminal. attempt_count never decreases and rises by at most one per update; changing the
    claimant requires a new attempt; a new attempt on a running job requires an expired lease.
  - `running` if and only if claimed_by and lease_expires_at are set; finished_at is set if and
    only if the status is terminal; `failed` has a failure code; `succeeded` has none.
  - A job is `succeeded` if and only if a result row exists (deferred constraint triggers, so the
    two writes are checked together at commit).
attempt_count is the fencing token: every worker write compares it (see services/job_state.py).
"""
import uuid
from datetime import datetime

from sqlalchemy import (CheckConstraint, DateTime, ForeignKey, Index, Integer, String, Text,
                        UniqueConstraint, Uuid, text)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from core.db.base import Base, TimestampMixin
from core.models.base import UuidPk


class Job(UuidPk, TimestampMixin, Base):
    __tablename__ = "job"
    __table_args__ = (
        UniqueConstraint("idempotency_key", name="uq_job_idempotency_key"),
        CheckConstraint("status IN ('queued', 'running', 'succeeded', 'failed')", name="status_valid"),
        CheckConstraint("max_attempts >= 1", name="max_attempts_positive"),
        CheckConstraint("attempt_count >= 0 AND attempt_count <= max_attempts", name="attempts_within_cap"),
        CheckConstraint("(status = 'running') = (claimed_by IS NOT NULL) AND "
                        "(status = 'running') = (lease_expires_at IS NOT NULL)", name="running_iff_claimed"),
        CheckConstraint("(status IN ('succeeded', 'failed')) = (finished_at IS NOT NULL)",
                        name="finished_iff_terminal"),
        CheckConstraint("status <> 'failed' OR failure_code IS NOT NULL", name="failed_has_code"),
        CheckConstraint("status <> 'succeeded' OR (failure_code IS NULL AND failure_message IS NULL)",
                        name="succeeded_has_no_failure"),
        CheckConstraint("(failure_code IS NULL) = (failure_message IS NULL)", name="failure_fields_together"),
        CheckConstraint("failure_code IS NULL OR failure_code ~ '^[a-z][a-z0-9_]{0,63}$'",
                        name="failure_code_format"),
        CheckConstraint("failure_message IS NULL OR char_length(failure_message) <= 500",
                        name="failure_message_length"),
        CheckConstraint("char_length(idempotency_key) BETWEEN 1 AND 255", name="idempotency_key_length"),
        CheckConstraint("control_variant <> ''", name="control_variant_not_empty"),
        CheckConstraint("char_length(config_hash) = 64", name="config_hash_length"),
        Index("ix_job_status", "status"),
    )

    status: Mapped[str] = mapped_column(String, nullable=False, server_default=text("'queued'"))
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    max_attempts: Mapped[int] = mapped_column(Integer, nullable=False)
    metric_version_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("metric_version.id", ondelete="RESTRICT", name="fk_job_metric_version_id_metric_version"),
        nullable=False)
    engine_version_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("engine_version.id", ondelete="RESTRICT", name="fk_job_engine_version_id_engine_version"),
        nullable=False)
    connection_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("connection.id", ondelete="RESTRICT", name="fk_job_connection_id_connection"),
        nullable=False)
    parameters: Mapped[dict] = mapped_column(JSONB, nullable=False)
    engine_params: Mapped[dict] = mapped_column(JSONB, nullable=False)
    control_variant: Mapped[str] = mapped_column(String, nullable=False)
    config_hash: Mapped[str] = mapped_column(String, nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String, nullable=False)
    claimed_by: Mapped[str | None] = mapped_column(String)
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    failure_code: Mapped[str | None] = mapped_column(String)
    failure_message: Mapped[str | None] = mapped_column(Text)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
