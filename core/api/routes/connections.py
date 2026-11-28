"""core/api/routes/connections.py: register and read connections.

Navigation:
  create_connection_route   POST /v1/connections
  get_connection_route      GET  /v1/connections/{id}

Invariants: the secret is write-only: it appears in no response, error, or log; the config is
validated by building the provider's connector (never opened) before anything is stored, so the
API never connects to a user's database; connections are immutable (register a new one to change
anything, Q17).
"""
import uuid

from fastapi import APIRouter, Request

from core.api.errors import ErrorOut
from core.api.schemas import ConnectionCreate, ConnectionOut
from core.db.session import session_scope
from core.services.connections import create_connection, get_connection

router = APIRouter(prefix="/v1/connections", tags=["connections"],
                   responses={404: {"model": ErrorOut}})


def _out(info) -> ConnectionOut:
    return ConnectionOut(id=info.id, name=info.name, type=info.type, config=info.config, created_at=info.created_at)


@router.post("", status_code=201, response_model=ConnectionOut)
def create_connection_route(body: ConnectionCreate, request: Request) -> ConnectionOut:
    with session_scope(request.app.state.deps.session_factory) as session:
        return _out(create_connection(session, body.name, body.type, body.config, body.secret))


@router.get("/{connection_id}", response_model=ConnectionOut)
def get_connection_route(connection_id: uuid.UUID, request: Request) -> ConnectionOut:
    with session_scope(request.app.state.deps.session_factory) as session:
        return _out(get_connection(session, connection_id))
