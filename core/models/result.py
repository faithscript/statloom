"""core/models/result.py: the one stored result of a job.

Navigation:
  Result   job_id (UNIQUE, NOT NULL), the validated engine output as JSON, its contract version

Invariants (migration 0003): exactly one result per job (UNIQUE(job_id): the backstop that makes a
repeated or duplicate write harmless); append-only; a result exists if and only if its job is
`succeeded` (deferred constraint triggers, checked at commit).
"""
import uuid

from sqlalchemy import ForeignKey, Integer, UniqueConstraint, Uuid
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from core.db.base import Base, TimestampMixin
from core.models.base import UuidPk


class Result(UuidPk, TimestampMixin, Base):
    __tablename__ = "result"
    __table_args__ = (UniqueConstraint("job_id", name="uq_result_job_id"),)

    job_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("job.id", ondelete="RESTRICT", name="fk_result_job_id_job"), nullable=False)
    output: Mapped[dict] = mapped_column(JSONB, nullable=False)
    contract_version: Mapped[int] = mapped_column(Integer, nullable=False)
