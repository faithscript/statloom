"""core/services/engine_registry.py: register and load immutable engine versions.

Navigation:
  create_engine_entry        a named engine (unique name)
  register_engine_version    validate the registered class -> same as latest? return it : insert next number
  load_pinned                a stored version -> (engine instance, PinnedEngine) for the runner

V1 engines are looked up by registry name (there is no entrypoint column). A version records
the name, the engine's declared version, and the contract version it was registered against.
Invariants: versions are never edited; identical content returns the existing version; an engine
is declared completely before it is registered. Does not prove the code is unchanged (Q13).
Never commits.
"""
import uuid

from sqlalchemy import select
from sqlalchemy.orm import Session

from core.engines import CONTRACT_VERSION, Engine as EngineBase, PinnedEngine, validate_engine
from core.engines.registry import create_engine, get_engine_class
from core.models import Engine, EngineVersion
from core.services.errors import AlreadyExists, NotFound
from core.services.versioning import latest_version


def create_engine_entry(session: Session, name: str) -> Engine:
    if session.scalar(select(Engine.id).where(Engine.name == name)) is not None:
        raise AlreadyExists("an engine with this name already exists")
    engine = Engine(name=name)
    session.add(engine)
    session.flush()
    return engine


def register_engine_version(session: Session, engine_id: uuid.UUID, engine_name: str) -> tuple[EngineVersion, bool]:
    """Returns (version, created). Raises UnknownEngine / EngineDeclarationInvalid (Component 4)."""
    cls = get_engine_class(engine_name)
    validate_engine(cls)
    latest = latest_version(session, Engine, engine_id)         # locks the engine row
    if (latest is not None and latest.engine_name == cls.name
            and latest.declared_version == cls.version and latest.contract_version == CONTRACT_VERSION):
        return latest, False
    version = EngineVersion(
        engine_id=engine_id, version_number=(latest.version_number if latest else 0) + 1,
        engine_name=cls.name, declared_version=cls.version, contract_version=CONTRACT_VERSION)
    session.add(version)
    session.flush()
    return version, True


def load_pinned(session: Session, engine_version_id: uuid.UUID) -> tuple[EngineBase, PinnedEngine]:
    v = session.get(EngineVersion, engine_version_id)
    if v is None:
        raise NotFound("engine version not found")
    return create_engine(v.engine_name), PinnedEngine(v.engine_name, v.declared_version, v.contract_version)
