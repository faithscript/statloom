"""core/api/schemas.py: request and response shapes. Nothing is exposed by default.

Navigation:
  MetricValidateRequest   a template, its parameter specs and type, optional sample values
  RenderedOut / MetricValidateOut   the validation result (content hash, optional rendering)
  HealthOut               liveness / readiness body

Requests forbid unknown fields and bound every collection. Responses are built field by
field: adding a column to a database model never adds a response field. A connection's
secret will be write-only (Phase B, with its route).
"""
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from core.metrics import MetricType, ParamSpec
from core.metrics.parameters import MAX_PARAMS
from core.metrics.template import MAX_TEMPLATE_BYTES


class _Request(BaseModel):
    model_config = ConfigDict(extra="forbid")  # invariant: unknown fields rejected


class MetricValidateRequest(_Request):
    sql_template: str = Field(min_length=1, max_length=MAX_TEMPLATE_BYTES)
    metric_type: MetricType
    parameters: list[ParamSpec] = Field(default_factory=list, max_length=MAX_PARAMS)
    sample_values: dict[str, Any] | None = Field(default=None, max_length=MAX_PARAMS)


class RenderedOut(BaseModel):
    sql: str
    params: dict[str, Any]


class MetricValidateOut(BaseModel):
    valid: bool
    content_hash: str
    rendered: RenderedOut | None = None


class HealthOut(BaseModel):
    status: str
