"""core/services/job_submission.py: create a job, pin everything, make resubmission idempotent.

Navigation:
  DEFAULT_MAX_ATTEMPTS   retry cap given to a new job (a parameter, not a decision about policy)
  compute_config_hash    SHA-256 of the canonical JSON of what was asked (not the key)
  submit_job             insert-or-find on the idempotency key -> (job, created)

Invariants: a job pins its metric version, engine version, connection, normalized parameters,
engine parameters, and control variant (the database refuses later changes); the same inputs always
give the same config hash; the same key with the same request returns the existing job and never
creates a second one, even when submitted concurrently (the unique index resolves the race in the
database); the same key with a *different* request raises IdempotencyConflict.
Parameters are stored as JSON (dates as ISO strings, datetimes in UTC), so the stored form is
deterministic. Never commits.
"""
import hashlib
import json
import uuid
from typing import Any, Mapping

from pydantic_core import to_jsonable_python
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from core.models import Connection, EngineVersion, Job, MetricVersion
from core.services.errors import IdempotencyConflict, NotFound

DEFAULT_MAX_ATTEMPTS = 3


def compute_config_hash(metric_version_id: uuid.UUID, engine_version_id: uuid.UUID, connection_id: uuid.UUID,
                        parameters: Mapping[str, Any], engine_params: Mapping[str, Any],
                        control_variant: str) -> str:
    payload = {
        "metric_version_id": str(metric_version_id), "engine_version_id": str(engine_version_id),
        "connection_id": str(connection_id), "parameters": to_jsonable_python(dict(parameters)),
        "engine_params": to_jsonable_python(dict(engine_params)), "control_variant": control_variant,
    }
    blob = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(blob.encode("ascii")).hexdigest()


def submit_job(session: Session, *, metric_version_id: uuid.UUID, engine_version_id: uuid.UUID,
               connection_id: uuid.UUID, parameters: Mapping[str, Any], engine_params: Mapping[str, Any],
               control_variant: str, idempotency_key: str,
               max_attempts: int = DEFAULT_MAX_ATTEMPTS) -> tuple[Job, bool]:
    """Returns (job, created). Raises NotFound for a missing reference, IdempotencyConflict on a
    key reused for a different request. `parameters` should already be normalized (Component 3)."""
    for model, id_, what in ((MetricVersion, metric_version_id, "metric version"),
                             (EngineVersion, engine_version_id, "engine version"),
                             (Connection, connection_id, "connection")):
        if session.get(model, id_) is None:
            raise NotFound(f"{what} not found")
    digest = compute_config_hash(metric_version_id, engine_version_id, connection_id, parameters,
                                 engine_params, control_variant)
    inserted_id = session.execute(
        pg_insert(Job).values(
            max_attempts=max_attempts, metric_version_id=metric_version_id, engine_version_id=engine_version_id,
            connection_id=connection_id, parameters=to_jsonable_python(dict(parameters)),
            engine_params=to_jsonable_python(dict(engine_params)), control_variant=control_variant,
            config_hash=digest, idempotency_key=idempotency_key)
        .on_conflict_do_nothing(index_elements=[Job.idempotency_key]).returning(Job.id)).scalar()
    if inserted_id is not None:
        return session.get(Job, inserted_id, populate_existing=True), True
    existing = session.scalars(select(Job).where(Job.idempotency_key == idempotency_key)
                               .execution_options(populate_existing=True)).one()
    if existing.config_hash != digest:
        raise IdempotencyConflict("this idempotency key was already used for a different request")
    return existing, False
