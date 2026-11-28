"""core/api/routes/health.py: liveness and readiness.

Navigation:
  make_router   /healthz (always 200) and /readyz (Postgres reachable and schema current)

Invariant: readiness is honest. The API says ready only if it can reach Postgres and the
database's migration state equals the code's, so a replica is never sent traffic it cannot
serve. Messages are generic: no connection URL or revision details leak.
"""
from fastapi import APIRouter
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from core.api.errors import ApiError, ErrorOut
from core.api.schemas import HealthOut
from core.db.session import SchemaMismatchError, check_schema_current


def make_router(deps) -> APIRouter:
    router = APIRouter(tags=["health"])

    @router.get("/healthz", response_model=HealthOut)
    def healthz() -> HealthOut:
        return HealthOut(status="ok")

    @router.get("/readyz", response_model=HealthOut, responses={503: {"model": ErrorOut}})
    def readyz() -> HealthOut:
        factory = deps.session_factory
        if factory is None:
            raise ApiError(503, "not_configured", "no database is configured")
        try:
            with factory() as session:
                session.execute(text("SELECT 1"))
            check_schema_current(factory.kw["bind"])
        except SchemaMismatchError:
            raise ApiError(503, "schema_mismatch", "database schema does not match the code") from None
        except SQLAlchemyError:
            raise ApiError(503, "service_unavailable", "database unreachable") from None
        return HealthOut(status="ready")

    return router
