"""core/engines: statistical engine contract, registry, runner, and reference engine (Component 4).

Public surface. Pure: no database, no connector. After the metric query, call
`run_engine(...)` with the job's pin, the query's columns and rows, and the control
variant; store `to_json(output)`; catch `EngineError` (always permanent).
The reference engine is imported below only so it registers itself.
"""
from core.engines.base import Engine, validate_engine
from core.engines.contract import (CONTRACT_VERSION, Comparison, EngineInput, EngineOutput,
                                   build_input, check_output)
from core.engines.errors import (DegenerateData, EngineCrashed, EngineDeclarationInvalid,
                                 EngineError, EngineMismatch, InputInvalid, InsufficientData,
                                 OutputInvalid, UnknownEngine)
from core.engines.registry import create_engine, register, registered_names
from core.engines.runner import PinnedEngine, run_engine, to_json

from core.engines import reference  # noqa: F401  (registers the "reference" engine)

__all__ = [
    "Engine", "validate_engine", "CONTRACT_VERSION", "Comparison", "EngineInput", "EngineOutput",
    "build_input", "check_output", "EngineError", "InputInvalid", "InsufficientData",
    "DegenerateData", "OutputInvalid", "EngineMismatch", "EngineCrashed", "UnknownEngine",
    "EngineDeclarationInvalid", "register", "create_engine", "registered_names",
    "PinnedEngine", "run_engine", "to_json",
]
