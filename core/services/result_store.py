"""core/services/result_store.py: store a job's one result, and read it back with its trace.

Navigation:
  complete_job             fenced running -> succeeded + insert the result (one transaction, the caller's)
  ResultTrace              a result with everything that produced it
  get_result_with_trace    read-only join; None if the job has no result yet

Invariants: a result exists if and only if its job is `succeeded` (also enforced by deferred database
triggers); one result per job (UNIQUE(job_id) is the backstop); a worker that no longer owns the job
(StaleClaim) stores nothing, because the fence is checked BEFORE the insert and the caller's
transaction rolls back on the exception. Never commits: the caller's session_scope commits, which
is what makes the result and the status change atomic.
"""
import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import select, text
from sqlalchemy.orm import Session

from core.models import Connection, Engine, EngineVersion, Job, Metric, MetricVersion, Result
from core.services.errors import NotFound, StaleClaim


def complete_job(session: Session, job_id: uuid.UUID, worker: str, attempt: int, output: dict[str, Any]) -> Result:
    """`output` is the validated engine output as JSON (core.engines.to_json); it must carry an
    integer `contract_version`."""
    contract_version = output.get("contract_version")
    if isinstance(contract_version, bool) or not isinstance(contract_version, int):
        raise ValueError("output must carry an integer contract_version")
    rid = session.execute(text(
        "UPDATE job SET status='succeeded', claimed_by=NULL, lease_expires_at=NULL, finished_at=now(), "
        "failure_code=NULL, failure_message=NULL "
        "WHERE id=:id AND status='running' AND claimed_by=:w AND attempt_count=:a RETURNING id"),
        {"id": job_id, "w": worker, "a": attempt}).scalar()
    if rid is None:
        if session.get(Job, job_id) is None:
            raise NotFound("job not found")
        raise StaleClaim("this worker no longer owns the job")
    result = Result(job_id=job_id, output=output, contract_version=contract_version)
    session.add(result)
    session.flush()
    session.get(Job, job_id, populate_existing=True)   # refresh the job: it changed under the ORM
    return result


@dataclass(frozen=True)
class ResultTrace:
    job_id: uuid.UUID
    output: dict[str, Any]
    contract_version: int
    metric_name: str
    metric_version_id: uuid.UUID
    metric_version_number: int
    metric_content_hash: str
    engine_name: str
    engine_version_id: uuid.UUID
    engine_version_number: int
    engine_registry_name: str
    engine_declared_version: str
    connection_id: uuid.UUID
    parameters: dict[str, Any]
    engine_params: dict[str, Any]
    control_variant: str
    config_hash: str
    attempt_count: int
    finished_at: datetime


def get_result_with_trace(session: Session, job_id: uuid.UUID) -> ResultTrace | None:
    if session.get(Job, job_id) is None:
        raise NotFound("job not found")
    row = session.execute(
        select(Result, Job, MetricVersion, Metric, EngineVersion, Engine, Connection)
        .join(Job, Job.id == Result.job_id)
        .join(MetricVersion, MetricVersion.id == Job.metric_version_id)
        .join(Metric, Metric.id == MetricVersion.metric_id)
        .join(EngineVersion, EngineVersion.id == Job.engine_version_id)
        .join(Engine, Engine.id == EngineVersion.engine_id)
        .join(Connection, Connection.id == Job.connection_id)
        .where(Result.job_id == job_id)).first()
    if row is None:
        return None
    result, job, mv, metric, ev, engine, connection = row
    return ResultTrace(
        job_id=job.id, output=result.output, contract_version=result.contract_version, metric_name=metric.name,
        metric_version_id=mv.id, metric_version_number=mv.version_number, metric_content_hash=mv.content_hash,
        engine_name=engine.name, engine_version_id=ev.id, engine_version_number=ev.version_number,
        engine_registry_name=ev.engine_name, engine_declared_version=ev.declared_version,
        connection_id=connection.id, parameters=job.parameters, engine_params=job.engine_params,
        control_variant=job.control_variant, config_hash=job.config_hash, attempt_count=job.attempt_count,
        finished_at=job.finished_at)
