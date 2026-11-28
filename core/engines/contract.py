"""core/engines/contract.py: the one input and output shape every engine uses.

Navigation:
  CONTRACT_VERSION  version of these shapes (stored with engine versions and results)
  EngineInput       validated observations per variant + the control variant + engine params
  Comparison        one treatment against the control (effect, SE, CI, p-value)
  EngineOutput      contract version + one Comparison per treatment
  build_input       query rows -> validated EngineInput (nothing is dropped or imputed)
  check_output      re-validate whatever an engine returned, then cross-check it against the input

Invariants: one contract for every engine; input validated before an engine runs;
output validated before anything stores it; output is plain JSON-able values.
Takes plain `columns` / `rows`, not a connector type, so it stays independent.
"""
import math
from dataclasses import dataclass
from decimal import Decimal
from typing import Annotated, Any, Mapping, Sequence

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from core.engines.errors import InputInvalid, InsufficientData, OutputInvalid
from core.metrics.contract import REQUIRED_COLUMNS, MetricType

CONTRACT_VERSION = 1
MIN_OBSERVATIONS = 2

Finite = Annotated[float, Field(allow_inf_nan=False)]


@dataclass(frozen=True)
class EngineInput:
    metric_type: MetricType
    control_variant: str
    samples: Mapping[str, tuple[float, ...]]   # variant -> observations, variants sorted
    params: BaseModel                          # the engine's own validated parameters


class Comparison(BaseModel):
    """One treatment against the control. For a proportion, `mean_*` is the observed rate."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    control: str
    treatment: str
    n_control: int = Field(gt=0)
    n_treatment: int = Field(gt=0)
    mean_control: Finite
    mean_treatment: Finite
    effect: Finite                  # treatment minus control
    effect_se: Finite = Field(ge=0)
    ci_lower: Finite
    ci_upper: Finite
    confidence_level: Finite = Field(gt=0, lt=1)
    p_value: Finite = Field(ge=0, le=1)
    method: str = Field(min_length=1)

    @model_validator(mode="after")
    def _ci_brackets_effect(self) -> "Comparison":
        if not self.ci_lower <= self.effect <= self.ci_upper:
            raise ValueError("confidence interval must bracket the effect")
        return self


class EngineOutput(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    contract_version: int
    comparisons: tuple[Comparison, ...] = Field(min_length=1, strict=False)  # JSON lists load back

    @model_validator(mode="after")
    def _version(self) -> "EngineOutput":
        if self.contract_version != CONTRACT_VERSION:
            raise ValueError("unsupported contract_version")
        return self


def _to_float(v: Any, metric_type: MetricType) -> float | None:
    """A finite float, or None if `v` is not an acceptable number for this metric type."""
    if isinstance(v, bool):
        out = float(v) if metric_type is MetricType.PROPORTION else None
    elif isinstance(v, (int, float, Decimal)):
        out = float(v)
    else:
        return None
    return out if out is not None and math.isfinite(out) else None


def build_input(metric_type: MetricType, columns: Sequence[str], rows: Sequence[Sequence[Any]],
                control_variant: str, params: BaseModel) -> EngineInput:
    """Validate query rows and group them by variant. Fails loudly: no value is ever
    dropped, imputed, or coerced from text. Messages never contain values."""
    missing = [c for c in REQUIRED_COLUMNS if c not in columns]
    if missing:
        raise InputInvalid(f"query result is missing required column(s): {', '.join(missing)}")
    vi, xi = list(columns).index("variant"), list(columns).index("value")
    ui = list(columns).index("unit_id") if "unit_id" in columns else None

    samples: dict[str, list[float]] = {}
    seen_units: set[Any] = set()
    for row in rows:
        variant = row[vi]
        if isinstance(variant, bool) or not isinstance(variant, (str, int)):
            raise InputInvalid("'variant' must be a non-null string or integer")
        variant = str(variant)
        x = _to_float(row[xi], metric_type)
        if x is None:
            raise InputInvalid(f"variant '{variant}': 'value' must be a finite number"
                               + (" (booleans allowed only for proportions)"
                                  if metric_type is MetricType.CONTINUOUS else ""))
        if metric_type is MetricType.PROPORTION and x not in (0.0, 1.0):
            raise InputInvalid(f"variant '{variant}': proportion values must be exactly 0 or 1")
        if ui is not None:
            unit = row[ui]
            if unit is None:
                raise InputInvalid(f"variant '{variant}': 'unit_id' must not be null")
            if unit in seen_units:
                raise InputInvalid(f"variant '{variant}': a unit appears more than once")
            seen_units.add(unit)
        samples.setdefault(variant, []).append(x)

    if control_variant not in samples:
        raise InputInvalid("the control variant has no rows")
    if len(samples) < 2:
        raise InputInvalid("at least one treatment variant besides the control is required")
    for variant, xs in samples.items():
        if len(xs) < MIN_OBSERVATIONS:
            raise InsufficientData(f"variant '{variant}' needs at least {MIN_OBSERVATIONS} observations")
    return EngineInput(metric_type=metric_type, control_variant=control_variant,
                       samples={k: tuple(samples[k]) for k in sorted(samples)}, params=params)


def check_output(output: Any, inp: EngineInput) -> EngineOutput:
    """Re-validate an engine's return value from scratch, then cross-check it against the
    input. Returns the validated output; raises OutputInvalid otherwise (never echoes values)."""
    if not isinstance(output, EngineOutput):
        raise OutputInvalid(f"engine must return EngineOutput, not {type(output).__name__}")
    try:  # model_construct() and subclasses can skip validation; dump and re-validate
        checked = EngineOutput.model_validate(output.model_dump())
    except ValidationError as e:
        problems = "; ".join(f"{'.'.join(map(str, err['loc']))}: {err['type']}" for err in e.errors())
        raise OutputInvalid(f"engine output breaks the contract ({problems})") from None
    treatments = sorted(v for v in inp.samples if v != inp.control_variant)
    if sorted(c.treatment for c in checked.comparisons) != treatments:
        raise OutputInvalid("engine output must have exactly one comparison per treatment variant")
    for c in checked.comparisons:
        if c.control != inp.control_variant:
            raise OutputInvalid("a comparison names the wrong control variant")
        if c.n_control != len(inp.samples[c.control]) or c.n_treatment != len(inp.samples[c.treatment]):
            raise OutputInvalid(f"sample counts for '{c.treatment}' do not match the input")
    return checked
