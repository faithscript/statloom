"""core/services/metric_registry.py: register and load immutable metric versions.

Navigation:
  create_metric              a named metric (unique name)
  register_metric_version    validate -> hash -> same as latest? return it : insert next number
  load_renderable            a stored version as plain values for a worker (read-only)
  RenderableMetric           what a worker needs to render and run a metric

Invariants: a version is never edited (a change is a new version; the database rejects UPDATE);
identical content returns the existing version, so numbers move only when content changes;
templates are validated by Component 3 before anything is stored. Never commits.
"""
import uuid
from dataclasses import dataclass
from typing import Sequence

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from core.metrics import MetricType, ParamSpec, content_hash, validate_template
from core.models import Metric, MetricVersion
from core.services.errors import AlreadyExists, NotFound
from core.services.versioning import latest_version


@dataclass(frozen=True)
class RenderableMetric:
    version_id: uuid.UUID
    sql_template: str
    specs: tuple[ParamSpec, ...]
    metric_type: MetricType
    content_hash: str


def create_metric(session: Session, name: str) -> Metric:
    """One atomic INSERT ... ON CONFLICT DO NOTHING: of two concurrent creations of one name,
    exactly one succeeds and the other raises AlreadyExists (never an integrity error)."""
    new_id = session.execute(pg_insert(Metric).values(name=name)
                             .on_conflict_do_nothing(index_elements=[Metric.name]).returning(Metric.id)).scalar()
    if new_id is None:
        raise AlreadyExists("a metric with this name already exists")
    return session.get(Metric, new_id, populate_existing=True)


def register_metric_version(session: Session, metric_id: uuid.UUID, sql: str,
                            specs: Sequence[ParamSpec], metric_type: MetricType) -> tuple[MetricVersion, bool]:
    """Returns (version, created). Raises MetricError (Component 3) on an invalid template."""
    validate_template(sql, specs)
    digest = content_hash(sql, specs, metric_type)
    latest = latest_version(session, Metric, metric_id)        # locks the metric row
    if latest is not None and latest.content_hash == digest:
        return latest, False
    version = MetricVersion(
        metric_id=metric_id, version_number=(latest.version_number if latest else 0) + 1,
        sql_template=sql, param_spec=[s.model_dump(mode="json") for s in specs],
        metric_type=MetricType(metric_type).value, content_hash=digest)
    session.add(version)
    session.flush()
    return version, True


def load_renderable(session: Session, metric_version_id: uuid.UUID) -> RenderableMetric:
    v = session.get(MetricVersion, metric_version_id)
    if v is None:
        raise NotFound("metric version not found")
    return RenderableMetric(
        version_id=v.id, sql_template=v.sql_template,
        specs=tuple(ParamSpec.model_validate(s) for s in v.param_spec),
        metric_type=MetricType(v.metric_type), content_hash=v.content_hash)


def get_metric_version(session: Session, metric_id: uuid.UUID, version_number: int) -> MetricVersion:
    v = session.scalars(select(MetricVersion).where(MetricVersion.metric_id == metric_id,
                                                    MetricVersion.version_number == version_number)).first()
    if v is None:
        raise NotFound("metric version not found")
    return v
