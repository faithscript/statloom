"""core/metrics/parameters.py: declared parameters, validation, and normalization.

Navigation:
  ParamType         string | integer | number | boolean | date | datetime | list
  ParamSpec         frozen declaration of one parameter
  validate_specs    registration-time checks (names, count, defaults)
  normalize_params  submission-time checks: coerce, fill defaults, sorted key order

Invariants: parameters are validated and bounded; defaults are valid; output is
deterministic. Error messages name the parameter, never its value.
"""
import math
import re
from datetime import date, datetime, timezone
from enum import Enum
from typing import Any, Mapping, Sequence

from pydantic import BaseModel, ConfigDict

from core.metrics.errors import ParameterInvalid

MAX_PARAMS = 50
DEFAULT_MAX_LENGTH = 1_000   # strings and lists
HARD_MAX_LENGTH = 10_000     # ceiling a spec may not exceed
_IDENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")


class ParamType(str, Enum):
    STRING = "string"
    INTEGER = "integer"
    NUMBER = "number"
    BOOLEAN = "boolean"
    DATE = "date"
    DATETIME = "datetime"
    LIST = "list"


_SCALARS = {t for t in ParamType if t is not ParamType.LIST}


class ParamSpec(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)  # invariant: unknown fields rejected

    name: str
    type: ParamType
    required: bool = True
    default: Any = None
    max_length: int | None = None            # strings and lists only
    element_type: ParamType | None = None    # lists only: the one scalar type


def _coerce_scalar(name: str, t: ParamType, value: Any, max_length: int) -> Any:
    if t is ParamType.STRING:
        if not isinstance(value, str):
            raise ParameterInvalid(f"parameter '{name}' must be a string")
        if len(value) > max_length:
            raise ParameterInvalid(f"parameter '{name}' exceeds max_length {max_length}")
        return value
    if t is ParamType.INTEGER:
        if isinstance(value, bool) or not isinstance(value, int):
            raise ParameterInvalid(f"parameter '{name}' must be an integer")
        return value
    if t is ParamType.NUMBER:
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
            raise ParameterInvalid(f"parameter '{name}' must be a finite number")
        return value
    if t is ParamType.BOOLEAN:
        if not isinstance(value, bool):
            raise ParameterInvalid(f"parameter '{name}' must be a boolean")
        return value
    if t is ParamType.DATE:
        if isinstance(value, str):
            try:
                return date.fromisoformat(value)
            except ValueError:
                raise ParameterInvalid(f"parameter '{name}' must be an ISO date") from None
        if isinstance(value, datetime) or not isinstance(value, date):
            raise ParameterInvalid(f"parameter '{name}' must be a date")
        return value
    if t is ParamType.DATETIME:
        if isinstance(value, str):
            try:
                value = datetime.fromisoformat(value)
            except ValueError:
                raise ParameterInvalid(f"parameter '{name}' must be an ISO datetime") from None
        if not isinstance(value, datetime):
            raise ParameterInvalid(f"parameter '{name}' must be a datetime")
        if value.tzinfo is None or value.utcoffset() is None:
            raise ParameterInvalid(f"parameter '{name}' must be timezone-aware")
        return value.astimezone(timezone.utc)
    raise ParameterInvalid(f"parameter '{name}' has an unsupported type")  # pragma: no cover


def _coerce(spec: ParamSpec, value: Any) -> Any:
    limit = spec.max_length or DEFAULT_MAX_LENGTH
    if spec.type is ParamType.LIST:
        if not isinstance(value, (list, tuple)):
            raise ParameterInvalid(f"parameter '{spec.name}' must be a list")
        if len(value) > limit:
            raise ParameterInvalid(f"parameter '{spec.name}' exceeds max_length {limit}")
        # elements are bounded by the element type's own rules (strings use the default cap)
        return [_coerce_scalar(spec.name, spec.element_type, v, DEFAULT_MAX_LENGTH) for v in value]
    return _coerce_scalar(spec.name, spec.type, value, limit)


def validate_specs(specs: Sequence[ParamSpec]) -> None:
    """Registration-time check. Invariants: bounded parameter count, unique valid
    names, and every default passes its own type check."""
    if len(specs) > MAX_PARAMS:
        raise ParameterInvalid(f"too many parameters (limit {MAX_PARAMS})")
    seen: set[str] = set()
    for s in specs:
        if not _IDENT.match(s.name):
            raise ParameterInvalid(f"parameter name {s.name!r} is not a valid identifier")
        if s.name in seen:
            raise ParameterInvalid(f"parameter '{s.name}' is declared twice")
        seen.add(s.name)
        if s.type is ParamType.LIST:
            if s.element_type not in _SCALARS:
                raise ParameterInvalid(f"list parameter '{s.name}' needs a scalar element_type")
        elif s.element_type is not None:
            raise ParameterInvalid(f"parameter '{s.name}': element_type is only for lists")
        if s.max_length is not None:
            if s.type not in (ParamType.STRING, ParamType.LIST):
                raise ParameterInvalid(f"parameter '{s.name}': max_length is only for strings and lists")
            if not 1 <= s.max_length <= HARD_MAX_LENGTH:
                raise ParameterInvalid(f"parameter '{s.name}': max_length must be 1..{HARD_MAX_LENGTH}")
        if s.default is not None:
            _coerce(s, s.default)  # raises ParameterInvalid naming the parameter


def normalize_params(specs: Sequence[ParamSpec], values: Mapping[str, Any]) -> dict[str, Any]:
    """Coerce values, fill defaults, reject unknown and missing, return sorted keys.

    An optional parameter with no value and no default is present as None, so a
    template can test `x is none`. Invariant: same input, same output, same order.
    """
    by_name = {s.name: s for s in specs}
    for k in values:
        if k not in by_name:
            raise ParameterInvalid(f"unknown parameter '{k}'")
    out: dict[str, Any] = {}
    for name in sorted(by_name):
        spec = by_name[name]
        value = values.get(name)
        if value is None:
            value = spec.default
        if value is None:
            if spec.required:
                raise ParameterInvalid(f"missing required parameter '{name}'")
            out[name] = None
        else:
            out[name] = _coerce(spec, value)
    return out
