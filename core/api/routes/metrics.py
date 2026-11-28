"""core/api/routes/metrics.py: metric endpoints.

Navigation:
  validate_metric                 POST /v1/metrics/validate: stateless dry run (no database)
  make_registration_router        create metrics, register and read versions (needs a database)

The dry run lets an author check a template before registering it: validate_template, the content
hash, and (given sample values) the exact SQL and bind dict a worker would run. Template and
parameter errors are Component 3's typed errors, mapped to 422. Registering an identical version
returns the existing one (200); changed content is a new version (201).
"""
import uuid

from fastapi import APIRouter, Request, Response

from core.api.errors import ErrorOut
from core.api.schemas import (MetricOut, MetricValidateOut, MetricValidateRequest, MetricVersionCreate,
                              MetricVersionOut, NameCreate, RenderedOut)
from core.db.session import session_scope
from core.metrics import content_hash, render, validate_template
from core.services.metric_registry import create_metric, get_metric_version, register_metric_version

router = APIRouter(prefix="/v1/metrics", tags=["metrics"])
registration_router = APIRouter(prefix="/v1/metrics", tags=["metrics"],
                                responses={404: {"model": ErrorOut}, 409: {"model": ErrorOut}})


@router.post("/validate", response_model=MetricValidateOut)
def validate_metric(body: MetricValidateRequest) -> MetricValidateOut:
    validate_template(body.sql_template, body.parameters)
    out = MetricValidateOut(valid=True,
                            content_hash=content_hash(body.sql_template, body.parameters, body.metric_type))
    if body.sample_values is not None:
        q = render(body.sql_template, body.parameters, body.sample_values)
        out.rendered = RenderedOut(sql=q.sql, params=q.params)
    return out


def _version_out(v) -> MetricVersionOut:
    return MetricVersionOut(id=v.id, metric_id=v.metric_id, version_number=v.version_number,
                            metric_type=v.metric_type, content_hash=v.content_hash, sql_template=v.sql_template,
                            parameters=v.param_spec, created_at=v.created_at)


@registration_router.post("", status_code=201, response_model=MetricOut)
def create_metric_route(body: NameCreate, request: Request) -> MetricOut:
    with session_scope(request.app.state.deps.session_factory) as session:
        m = create_metric(session, body.name)
        return MetricOut(id=m.id, name=m.name, created_at=m.created_at)


@registration_router.post("/{metric_id}/versions", status_code=201, response_model=MetricVersionOut)
def create_metric_version_route(metric_id: uuid.UUID, body: MetricVersionCreate, request: Request,
                                response: Response) -> MetricVersionOut:
    with session_scope(request.app.state.deps.session_factory) as session:
        version, created = register_metric_version(session, metric_id, body.sql_template, body.parameters,
                                                   body.metric_type)
        out = _version_out(version)
    if not created:
        response.status_code = 200
    return out


@registration_router.get("/{metric_id}/versions/{version_number}", response_model=MetricVersionOut)
def get_metric_version_route(metric_id: uuid.UUID, version_number: int, request: Request) -> MetricVersionOut:
    with session_scope(request.app.state.deps.session_factory) as session:
        return _version_out(get_metric_version(session, metric_id, version_number))
