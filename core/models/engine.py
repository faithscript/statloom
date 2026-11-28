"""core/models/engine.py: a named engine and its immutable versions.

Navigation:
  Engine         id + unique name
  EngineVersion  registry name, declared version, contract version; UNIQUE(engine_id, version_number)

V1 engines are looked up by registry name, so there is no entrypoint column (revisit with Q13,
engine code identity). Invariants (migration 0002): append-only; unique version numbers per engine;
ON DELETE RESTRICT while a job references a version.
"""
import uuid

from sqlalchemy import CheckConstraint, ForeignKey, Integer, String, UniqueConstraint, Uuid
from sqlalchemy.orm import Mapped, mapped_column

from core.db.base import Base, TimestampMixin
from core.models.base import UuidPk


class Engine(UuidPk, TimestampMixin, Base):
    __tablename__ = "engine"
    __table_args__ = (UniqueConstraint("name", name="uq_engine_name"),)

    name: Mapped[str] = mapped_column(String, nullable=False)


class EngineVersion(UuidPk, TimestampMixin, Base):
    __tablename__ = "engine_version"
    __table_args__ = (
        UniqueConstraint("engine_id", "version_number", name="uq_engine_version_engine_id_version_number"),
        CheckConstraint("version_number >= 1", name="version_number_positive"),
        CheckConstraint("engine_name <> '' AND declared_version <> ''", name="name_and_version_not_empty"),
    )

    engine_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("engine.id", ondelete="RESTRICT", name="fk_engine_version_engine_id_engine"), nullable=False)
    version_number: Mapped[int] = mapped_column(Integer, nullable=False)
    engine_name: Mapped[str] = mapped_column(String, nullable=False)
    declared_version: Mapped[str] = mapped_column(String, nullable=False)
    contract_version: Mapped[int] = mapped_column(Integer, nullable=False)
