"""core/metrics: metric templates, parameters, and the output contract (Component 3).

Public surface. Pure: no database, no provider. Registering is `validate_template`;
running is `render(...)` -> connector `run_query` -> `check_columns(result.columns)`.
"""
from core.metrics.contract import OPTIONAL_COLUMNS, REQUIRED_COLUMNS, MetricType, check_columns
from core.metrics.errors import (MetricError, OutputContractViolation, ParameterInvalid,
                                 TemplateInvalid)
from core.metrics.parameters import ParamSpec, ParamType, normalize_params, validate_specs
from core.metrics.template import RenderedQuery, content_hash, render, validate_template

__all__ = [
    "MetricError", "TemplateInvalid", "ParameterInvalid", "OutputContractViolation",
    "ParamSpec", "ParamType", "validate_specs", "normalize_params",
    "validate_template", "render", "RenderedQuery", "content_hash",
    "MetricType", "REQUIRED_COLUMNS", "OPTIONAL_COLUMNS", "check_columns",
]
