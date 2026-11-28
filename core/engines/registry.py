"""core/engines/registry.py: maps an engine name to an engine class.

Navigation:
  register           class decorator; validates the declaration, rejects duplicates
  unregister         remove an engine (used by tests)
  get_engine_class   lookup; raises UnknownEngine
  create_engine      name in, engine instance out
  registered_names   sorted list of known names

Invariants: pluggable (a new engine is one decorated class, no core edits) and strict
(incomplete declarations, duplicate names, and unknown names raise typed errors).
Same shape as core/connectors/registry.py.
"""
from typing import Callable

from core.engines.base import Engine, validate_engine
from core.engines.errors import EngineDeclarationInvalid, UnknownEngine

_REGISTRY: dict[str, type[Engine]] = {}


def register(name: str) -> Callable[[type[Engine]], type[Engine]]:
    def decorator(cls: type[Engine]) -> type[Engine]:
        validate_engine(cls)
        if cls.name != name:
            raise EngineDeclarationInvalid(f"engine declares name {cls.name!r} but is registered as {name!r}")
        if name in _REGISTRY:
            raise EngineDeclarationInvalid(f"engine {name!r} is already registered")
        _REGISTRY[name] = cls
        return cls

    return decorator


def unregister(name: str) -> None:
    _REGISTRY.pop(name, None)


def get_engine_class(name: str) -> type[Engine]:
    try:
        return _REGISTRY[name]
    except KeyError:
        raise UnknownEngine(f"no engine registered as {name!r}; known: {sorted(_REGISTRY)}") from None


def create_engine(name: str) -> Engine:
    return get_engine_class(name)()


def registered_names() -> list[str]:
    return sorted(_REGISTRY)
