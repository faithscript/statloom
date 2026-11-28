"""core/api/schemas.py: request and response shapes. Nothing is exposed by default.

Navigation:
  Requests    MetricValidateRequest, ConnectionCreate (secret is write-only), NameCreate,
              MetricVersionCreate, EngineVersionCreate, JobSubmit
  Responses   ConnectionOut, MetricOut, MetricVersionOut, EngineOut, EngineVersionOut, JobOut, ResultOut,
              MetricValidateOut, HealthOut

Requests forbid unknown fields and bound every collection. Responses are built field by
field: adding a column to a database model never adds a response field. No response has a
secret, an encrypted token, a worker id, or a lease.
"""
import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, SecretStr

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


class ConnectionCreate(_Request):
    name: str = Field(min_length=1, max_length=200)
    type: str = Field(min_length=1, max_length=64)
    config: dict[str, Any] = Field(max_length=50)
    secret: SecretStr = Field(min_length=1, max_length=4096)      # write-only: never in a response


class ConnectionOut(BaseModel):
    id: uuid.UUID
    name: str
    type: str
    config: dict[str, Any]
    created_at: datetime


class NameCreate(_Request):
    name: str = Field(min_length=1, max_length=200)


class MetricOut(BaseModel):
    id: uuid.UUID
    name: str
    created_at: datetime


class MetricVersionCreate(_Request):
    sql_template: str = Field(min_length=1, max_length=MAX_TEMPLATE_BYTES)
    metric_type: MetricType
    parameters: list[ParamSpec] = Field(default_factory=list, max_length=MAX_PARAMS)


class MetricVersionOut(BaseModel):
    id: uuid.UUID
    metric_id: uuid.UUID
    version_number: int
    metric_type: MetricType
    content_hash: str
    sql_template: str
    parameters: list[dict[str, Any]]
    created_at: datetime


class EngineOut(BaseModel):
    id: uuid.UUID
    name: str
    created_at: datetime


class EngineVersionCreate(_Request):
    engine_name: str = Field(min_length=1, max_length=200)


class EngineVersionOut(BaseModel):
    id: uuid.UUID
    engine_id: uuid.UUID
    version_number: int
    engine_name: str
    declared_version: str
    contract_version: int
    created_at: datetime


class JobSubmit(_Request):
    metric_version_id: uuid.UUID
    engine_version_id: uuid.UUID
    connection_id: uuid.UUID
    parameters: dict[str, Any] = Field(max_length=MAX_PARAMS)
    engine_params: dict[str, Any] = Field(default_factory=dict, max_length=MAX_PARAMS)
    control_variant: str = Field(min_length=1, max_length=200)


class FailureOut(BaseModel):
    code: str
    message: str


class JobOut(BaseModel):
    id: uuid.UUID
    status: str
    attempt_count: int
    max_attempts: int
    metric_version_id: uuid.UUID
    engine_version_id: uuid.UUID
    connection_id: uuid.UUID
    parameters: dict[str, Any]
    engine_params: dict[str, Any]
    control_variant: str
    config_hash: str
    failure: FailureOut | None = None
    created_at: datetime
    started_at: datetime | None = None
    finished_at: datetime | None = None


class TraceOut(BaseModel):
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


class ResultOut(BaseModel):
    job_id: uuid.UUID
    contract_version: int
    result: dict[str, Any]
    trace: TraceOut
