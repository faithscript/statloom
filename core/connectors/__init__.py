"""core/connectors: the database connector layer (Component 2).

Public surface for the rest of Statloom: build a ConnectionSpec, call
create_connector(spec), then use the Connector interface. Provider modules are
imported below only so they register themselves; callers never import them.
"""
from core.connectors.base import ConnectionSpec, Connector, QueryResult
from core.connectors.errors import (AuthenticationFailed, ConfigInvalid, ConnectionFailed,
                                    ConnectorError, QueryRejected, QueryTimeout,
                                    ResultTooLarge, UnknownConnectorType)
from core.connectors.registry import create_connector, register, registered_types

from core.connectors import postgres  # noqa: F401  (registers the "postgres" provider)

__all__ = [
    "ConnectionSpec", "Connector", "QueryResult", "create_connector", "register",
    "registered_types", "ConnectorError", "ConnectionFailed", "QueryTimeout",
    "AuthenticationFailed", "QueryRejected", "ResultTooLarge", "ConfigInvalid",
    "UnknownConnectorType",
]
