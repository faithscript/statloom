"""core/api/routes/jobs.py: submit a job, read its status, fetch its result.

Navigation:
  submit_job_route    POST /v1/jobs      validate -> store (one transaction) -> commit -> publish
  get_job_route       GET  /v1/jobs/{id}
  get_result_route    GET  /v1/jobs/{id}/result

Submit, in order:
  1. Bound the body and the Idempotency-Key header (client-supplied key; Q4).
  2. In ONE transaction: load the pinned metric version, engine version, and connection (404 if
     absent); check the engine supports the metric type; normalize parameters against the metric
     version's specs; validate the engine's own parameters (stored as the validated model dumped to
     JSON, defaults filled in); call submit_job (atomic insert-or-find on the key). Commit.
  3. AFTER the commit, if the job was newly created, publish its id. Any exception from the
     publisher is treated as a failed publish: logged by type only, the job stays `queued`, and the
     client still gets 202. This is the dual-write gap (Q1): visible, documented, not closed.
  4. Respond 202 with the job and a Location header. A replay returns the same job with
     Idempotent-Replayed: true and does NOT publish again. The same key with a different request
     is a 409.
Nothing here runs an analysis, a query, or an engine.
"""
import logging
import uuid
from typing import Annotated

from fastapi import APIRouter, Header, Request, Response
from pydantic import ValidationError

from core.api.errors import ApiError, ErrorOut
from core.api.schemas import FailureOut, JobOut, JobSubmit, ResultOut, TraceOut
from core.db.session import session_scope
from core.metrics import normalize_params
from core.services.connections import get_connection
from core.services.engine_registry import load_pinned
from core.services.job_state import get_job
from core.services.job_submission import submit_job
from core.services.metric_registry import load_renderable
from core.services.result_store import get_result_with_trace

log = logging.getLogger("statloom.api")
router = APIRouter(prefix="/v1/jobs", tags=["jobs"],
                   responses={404: {"model": ErrorOut}, 409: {"model": ErrorOut}})

IdempotencyKey = Annotated[str, Header(alias="Idempotency-Key", min_length=1, max_length=255,
                                       pattern=r"^[\x21-\x7e]+$")]


def _job_out(job) -> JobOut:
    failure = FailureOut(code=job.failure_code, message=job.failure_message) if job.failure_code else None
    return JobOut(
        id=job.id, status=job.status, attempt_count=job.attempt_count, max_attempts=job.max_attempts,
        metric_version_id=job.metric_version_id, engine_version_id=job.engine_version_id,
        connection_id=job.connection_id, parameters=job.parameters, engine_params=job.engine_params,
        control_variant=job.control_variant, config_hash=job.config_hash, failure=failure,
        created_at=job.created_at, started_at=job.started_at, finished_at=job.finished_at)


@router.post("", status_code=202, response_model=JobOut)
def submit_job_route(body: JobSubmit, request: Request, response: Response,
                     idempotency_key: IdempotencyKey) -> JobOut:
    deps = request.app.state.deps
    with session_scope(deps.session_factory) as session:
        metric = load_renderable(session, body.metric_version_id)
        engine, _pin = load_pinned(session, body.engine_version_id)
        get_connection(session, body.connection_id)                       # 404 if it does not exist
        if metric.metric_type not in engine.supports:
            raise ApiError(422, "engine_metric_mismatch",
                           f"the engine does not support {metric.metric_type.value} metrics")
        parameters = normalize_params(metric.specs, body.parameters)
        try:
            engine_params = engine.params_model.model_validate(body.engine_params).model_dump(mode="json")
        except ValidationError as e:
            fields = ", ".join(sorted({".".join(map(str, err["loc"])) or "<root>" for err in e.errors()}))
            raise ApiError(422, "engine_params_invalid", f"invalid engine parameter(s): {fields}") from None
        job, created = submit_job(
            session, metric_version_id=body.metric_version_id, engine_version_id=body.engine_version_id,
            connection_id=body.connection_id, parameters=parameters, engine_params=engine_params,
            control_variant=body.control_variant, idempotency_key=idempotency_key)
        out = _job_out(job)
    # The transaction above is committed here. Only now is the id published (Q1: the gap is the time
    # between this line and a successful publish).
    if created:
        if deps.publisher is None:
            log.warning("no publisher configured; job stays queued rid=%s", request.state.request_id)
        else:
            try:
                deps.publisher.publish(str(job.id))
            except Exception as exc:  # noqa: BLE001  any publisher failure: the job is committed, answer the client
                log.error("publish failed (%s); job stays queued rid=%s", type(exc).__name__,
                          request.state.request_id)
    else:
        response.headers["Idempotent-Replayed"] = "true"
    response.headers["Location"] = f"/v1/jobs/{out.id}"
    return out


@router.get("/{job_id}", response_model=JobOut)
def get_job_route(job_id: uuid.UUID, request: Request) -> JobOut:
    with session_scope(request.app.state.deps.session_factory) as session:
        return _job_out(get_job(session, job_id))


@router.get("/{job_id}/result", response_model=ResultOut)
def get_result_route(job_id: uuid.UUID, request: Request) -> ResultOut:
    with session_scope(request.app.state.deps.session_factory) as session:
        job = get_job(session, job_id)
        if job.status == "failed":
            raise ApiError(409, "job_failed", f"the job failed with {job.failure_code}")
        trace = get_result_with_trace(session, job_id) if job.status == "succeeded" else None
        if trace is None:
            raise ApiError(409, "result_not_ready", f"the job is {job.status}")
        t = trace
        return ResultOut(
            job_id=t.job_id, contract_version=t.contract_version, result=t.output,
            trace=TraceOut(
                metric_name=t.metric_name, metric_version_id=t.metric_version_id,
                metric_version_number=t.metric_version_number, metric_content_hash=t.metric_content_hash,
                engine_name=t.engine_name, engine_version_id=t.engine_version_id,
                engine_version_number=t.engine_version_number, engine_registry_name=t.engine_registry_name,
                engine_declared_version=t.engine_declared_version, connection_id=t.connection_id,
                parameters=t.parameters, engine_params=t.engine_params, control_variant=t.control_variant,
                config_hash=t.config_hash, attempt_count=t.attempt_count, finished_at=t.finished_at))
