"""core/api/routes/engines.py: register engines and engine versions.

Navigation:
  create_engine_route, create_engine_version_route, get_engine_version_route

An engine version names a class that must be installed in this image (the registry validates it;
Q16: the worker image must have the same engines). Identical declarations return the existing
version (200); a new declaration is a new version (201).
"""
import uuid

from fastapi import APIRouter, Request, Response

from core.api.errors import ErrorOut
from core.api.schemas import EngineOut, EngineVersionCreate, EngineVersionOut, NameCreate
from core.db.session import session_scope
from core.services.engine_registry import (create_engine_entry, get_engine_version, register_engine_version)

router = APIRouter(prefix="/v1/engines", tags=["engines"],
                   responses={404: {"model": ErrorOut}, 409: {"model": ErrorOut}})


def _version_out(v) -> EngineVersionOut:
    return EngineVersionOut(id=v.id, engine_id=v.engine_id, version_number=v.version_number,
                            engine_name=v.engine_name, declared_version=v.declared_version,
                            contract_version=v.contract_version, created_at=v.created_at)


@router.post("", status_code=201, response_model=EngineOut)
def create_engine_route(body: NameCreate, request: Request) -> EngineOut:
    with session_scope(request.app.state.deps.session_factory) as session:
        e = create_engine_entry(session, body.name)
        return EngineOut(id=e.id, name=e.name, created_at=e.created_at)


@router.post("/{engine_id}/versions", status_code=201, response_model=EngineVersionOut)
def create_engine_version_route(engine_id: uuid.UUID, body: EngineVersionCreate, request: Request,
                                response: Response) -> EngineVersionOut:
    with session_scope(request.app.state.deps.session_factory) as session:
        version, created = register_engine_version(session, engine_id, body.engine_name)
        out = _version_out(version)
    if not created:
        response.status_code = 200
    return out


@router.get("/{engine_id}/versions/{version_number}", response_model=EngineVersionOut)
def get_engine_version_route(engine_id: uuid.UUID, version_number: int, request: Request) -> EngineVersionOut:
    with session_scope(request.app.state.deps.session_factory) as session:
        return _version_out(get_engine_version(session, engine_id, version_number))
