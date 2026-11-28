"""core/services/connections.py: register connections and build connector specs.

Navigation:
  ConnectionInfo         a connection WITHOUT its secret (what callers and the API may see)
  create_connection      validate config (build a connector, never open it), encrypt, store
  get_connection         read a connection as ConnectionInfo
  load_connection_spec   WORKER ONLY: a ConnectionSpec with the decrypted secret

Invariants: a credential is stored only encrypted and returned in plaintext nowhere except a
masked SecretStr inside the ConnectionSpec that load_connection_spec builds for a connector;
config is validated by the provider before anything is stored; connections are append-only
(Q17: to change a host or rotate a secret, register a new connection). Never commits.
"""
import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from pydantic import SecretStr
from sqlalchemy.orm import Session

from core.connectors import ConnectionSpec, create_connector
from core.models import Connection
from core.security import decrypt, encrypt
from core.services.errors import NotFound


@dataclass(frozen=True)
class ConnectionInfo:
    id: uuid.UUID
    name: str
    type: str
    config: dict[str, Any]
    created_at: datetime


def _info(row: Connection) -> ConnectionInfo:
    return ConnectionInfo(id=row.id, name=row.name, type=row.type, config=dict(row.config),
                          created_at=row.created_at)


def create_connection(session: Session, name: str, type_: str, config: dict[str, Any],
                      secret: SecretStr) -> ConnectionInfo:
    """Raises ConfigInvalid / UnknownConnectorType (Component 2) before storing anything."""
    create_connector(ConnectionSpec(type=type_, config=config, secret=secret))  # validates; never opens
    row = Connection(name=name, type=type_, config=config, encrypted_secret=encrypt(secret))
    session.add(row)
    session.flush()
    return _info(row)


def _row(session: Session, connection_id: uuid.UUID) -> Connection:
    row = session.get(Connection, connection_id)
    if row is None:
        raise NotFound("connection not found")
    return row


def get_connection(session: Session, connection_id: uuid.UUID) -> ConnectionInfo:
    return _info(_row(session, connection_id))


def load_connection_spec(session: Session, connection_id: uuid.UUID) -> ConnectionSpec:
    row = _row(session, connection_id)
    return ConnectionSpec(type=row.type, config=dict(row.config), secret=decrypt(row.encrypted_secret))
