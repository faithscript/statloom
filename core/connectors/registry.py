"""core/connectors/registry.py: maps a connection type name to a connector class.

Navigation:
  register               class decorator that adds a provider
  unregister             remove a provider (used by tests)
  get_connector_class    lookup; raises UnknownConnectorType
  create_connector       THE entry point core code uses: spec in, connector out
  registered_types       list of known type names

Invariants: pluggable (a new provider is one decorated class, no core edits) and
strict (duplicate names rejected, unknown names raise a typed error).
"""
from typing import Callable

from core.connectors.base import ConnectionSpec, Connector
from core.connectors.errors import ConfigInvalid, UnknownConnectorType

_REGISTRY: dict[str, type[Connector]] = {}


def register(type_name: str) -> Callable[[type[Connector]], type[Connector]]:
    def decorator(cls: type[Connector]) -> type[Connector]:
        if type_name in _REGISTRY:
            raise ConfigInvalid(f"connector type {type_name!r} is already registered")
        _REGISTRY[type_name] = cls
        return cls

    return decorator


def unregister(type_name: str) -> None:
    _REGISTRY.pop(type_name, None)


def get_connector_class(type_name: str) -> type[Connector]:
    try:
        return _REGISTRY[type_name]
    except KeyError:
        raise UnknownConnectorType(
            f"no connector registered for {type_name!r}; known: {sorted(_REGISTRY)}"
        ) from None


def create_connector(spec: ConnectionSpec) -> Connector:
    return get_connector_class(spec.type)(spec)


def registered_types() -> list[str]:
    return sorted(_REGISTRY)
