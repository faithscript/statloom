"""core/models/metric.py: a named metric and its immutable versions.

Navigation:
  Metric         id + unique name
  MetricVersion  template, parameter specs, type, content hash; UNIQUE(metric_id, version_number)

Invariants (migration 0002): both tables are append-only; version numbers are unique per
metric (gaplessness comes from services/versioning.py under a row lock); a version cannot be
deleted while a job references it (ON DELETE RESTRICT).
"""
import uuid

from sqlalchemy import CheckConstraint, ForeignKey, Integer, String, Text, UniqueConstraint, Uuid
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from core.db.base import Base, TimestampMixin
from core.models.base import UuidPk


class Metric(UuidPk, TimestampMixin, Base):
    __tablename__ = "metric"
    __table_args__ = (UniqueConstraint("name", name="uq_metric_name"),)

    name: Mapped[str] = mapped_column(String, nullable=False)


class MetricVersion(UuidPk, TimestampMixin, Base):
    __tablename__ = "metric_version"
    __table_args__ = (
        UniqueConstraint("metric_id", "version_number", name="uq_metric_version_metric_id_version_number"),
        CheckConstraint("version_number >= 1", name="version_number_positive"),
        CheckConstraint("metric_type IN ('continuous', 'proportion')", name="metric_type_valid"),
        CheckConstraint("char_length(content_hash) = 64", name="content_hash_length"),
    )

    metric_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("metric.id", ondelete="RESTRICT", name="fk_metric_version_metric_id_metric"), nullable=False)
    version_number: Mapped[int] = mapped_column(Integer, nullable=False)
    sql_template: Mapped[str] = mapped_column(Text, nullable=False)
    param_spec: Mapped[list] = mapped_column(JSONB, nullable=False)
    metric_type: Mapped[str] = mapped_column(String, nullable=False)
    content_hash: Mapped[str] = mapped_column(String, nullable=False)
