"""core/engines/runner.py: the only way the worker runs an engine.

Navigation:
  PinnedEngine  what a job pinned: engine name, declared version, contract version
  run_engine    version check -> params -> validated input -> engine.run -> validated output
  to_json       the validated output as plain JSON values (what Component 1 stores)

Engine code is trusted in V1; engine output is not. A buggy or hurried custom engine
therefore fails one job loudly instead of persisting a plausible wrong number.
In-process, no timeout (Q6).
"""
from dataclasses import dataclass
from typing import Any, Mapping, Sequence

from pydantic import ValidationError

from core.engines.base import Engine
from core.engines.contract import CONTRACT_VERSION, EngineOutput, build_input, check_output
from core.engines.errors import EngineCrashed, EngineError, EngineMismatch, InputInvalid
from core.metrics.contract import MetricType


@dataclass(frozen=True)
class PinnedEngine:
    name: str
    version: str
    contract_version: int = CONTRACT_VERSION


def run_engine(engine: Engine, pinned: PinnedEngine, metric_type: MetricType,
               columns: Sequence[str], rows: Sequence[Sequence[Any]], control_variant: str,
               raw_params: Mapping[str, Any] | None = None) -> EngineOutput:
    if (engine.name, engine.version) != (pinned.name, pinned.version):
        raise EngineMismatch("loaded engine does not match the pinned engine name and version")
    if pinned.contract_version != CONTRACT_VERSION:
        raise EngineMismatch("pinned contract version is not supported by this build")
    if metric_type not in engine.supports:
        raise EngineMismatch(f"engine '{engine.name}' does not support {MetricType(metric_type).value} metrics")
    try:
        params = engine.params_model.model_validate(dict(raw_params or {}))
    except ValidationError as e:
        fields = ", ".join(sorted({".".join(map(str, err["loc"])) or "<root>" for err in e.errors()}))
        raise InputInvalid(f"invalid engine parameter(s): {fields}") from None
    inp = build_input(metric_type, columns, rows, control_variant, params)
    try:
        result = engine.run(inp)
    except EngineError:
        raise
    except Exception as e:  # noqa: BLE001  any engine bug becomes a typed, data-free error
        raise EngineCrashed(f"engine '{engine.name}' raised {type(e).__name__}") from e
    return check_output(result, inp)


def to_json(output: EngineOutput) -> dict[str, Any]:
    return output.model_dump(mode="json")
