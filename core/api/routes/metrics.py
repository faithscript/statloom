"""core/api/routes/metrics.py: metric endpoints.

Navigation:
  validate_metric   POST /v1/metrics/validate: stateless dry run (no database)

Registering metrics and versions (Phase B) waits for Component 1.

The dry run lets an author check a template before registering it: validate_template,
the content hash, and (given sample values) the exact SQL and bind dict a worker would
run. Template and parameter errors are Component 3's typed errors, mapped to 422.
"""
from fastapi import APIRouter

from core.api.schemas import MetricValidateOut, MetricValidateRequest, RenderedOut
from core.metrics import content_hash, render, validate_template

router = APIRouter(prefix="/v1/metrics", tags=["metrics"])


@router.post("/validate", response_model=MetricValidateOut)
def validate_metric(body: MetricValidateRequest) -> MetricValidateOut:
    validate_template(body.sql_template, body.parameters)
    out = MetricValidateOut(valid=True,
                            content_hash=content_hash(body.sql_template, body.parameters, body.metric_type))
    if body.sample_values is not None:
        q = render(body.sql_template, body.parameters, body.sample_values)
        out.rendered = RenderedOut(sql=q.sql, params=q.params)
    return out
