"""core/services/versioning.py: immutable, gapless version numbers.

Navigation:
  latest_version        lock the parent row, return its newest version (or None)
  next_version_number   latest + 1

Invariant: concurrent registrations never produce duplicate or skipped numbers. Every
registration first takes `SELECT ... FOR UPDATE` on the parent (metric or engine) row, so
registrations for one entity are serialized; UNIQUE(parent, version_number) is the backstop.
Must be called inside session_scope; this module never commits.
"""
import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from core.db.session import lock_row
from core.models import Engine, EngineVersion, Metric, MetricVersion
from core.services.errors import NotFound

_VERSION_OF: dict[type, tuple[type, str]] = {
    Metric: (MetricVersion, "metric_id"),
    Engine: (EngineVersion, "engine_id"),
}


def latest_version(session: Session, parent_model: type, parent_id: uuid.UUID) -> Any | None:
    try:
        lock_row(session, parent_model, parent_id)
    except LookupError:
        raise NotFound(f"{parent_model.__name__.lower()} not found") from None
    version_model, fk = _VERSION_OF[parent_model]
    return session.scalars(
        select(version_model).where(getattr(version_model, fk) == parent_id)
        .order_by(version_model.version_number.desc()).limit(1)).first()


def next_version_number(session: Session, parent_model: type, parent_id: uuid.UUID) -> int:
    latest = latest_version(session, parent_model, parent_id)
    return (latest.version_number if latest else 0) + 1
