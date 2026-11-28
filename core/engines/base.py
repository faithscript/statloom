"""core/engines/base.py: the interface every statistical engine implements.

Navigation:
  Engine           ABC: name, version, supports, params_model, run()
  validate_engine  registration-time completeness check

An engine is a pure function of its EngineInput: it gets data and returns data. It is
never handed a connector, session, or secret. Engine code is trusted in V1;
engine output is not (see runner.py).
"""
from abc import ABC, abstractmethod
from typing import ClassVar

from pydantic import BaseModel

from core.engines.contract import EngineInput, EngineOutput
from core.engines.errors import EngineDeclarationInvalid
from core.metrics.contract import MetricType


class Engine(ABC):
    name: ClassVar[str]
    version: ClassVar[str]
    supports: ClassVar[frozenset[MetricType]]
    params_model: ClassVar[type[BaseModel]]   # the engine's own parameters (extra="forbid")

    @abstractmethod
    def run(self, inp: EngineInput) -> EngineOutput: ...


def validate_engine(cls: type) -> None:
    """Invariant: an engine declares itself completely, so mistakes surface at
    registration and not inside a worker."""
    if not (isinstance(cls, type) and issubclass(cls, Engine)):
        raise EngineDeclarationInvalid("an engine must subclass Engine")
    for attr in ("name", "version"):
        v = getattr(cls, attr, None)
        if not isinstance(v, str) or not v:
            raise EngineDeclarationInvalid(f"engine must declare a non-empty string '{attr}'")
    sup = getattr(cls, "supports", None)
    if (not isinstance(sup, (set, frozenset)) or not sup
            or not all(isinstance(m, MetricType) for m in sup)):
        raise EngineDeclarationInvalid("engine 'supports' must be a non-empty set of MetricType")
    pm = getattr(cls, "params_model", None)
    if not (isinstance(pm, type) and issubclass(pm, BaseModel)):
        raise EngineDeclarationInvalid("engine 'params_model' must be a pydantic model")
    if getattr(cls, "__abstractmethods__", None):
        raise EngineDeclarationInvalid("engine must implement run()")
