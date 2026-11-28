"""core/api/errors.py: one error shape and one table mapping failures to HTTP.

Navigation:
  ApiError          raised by routes for failures the API itself detects
  error_response    build the single error body (code, message, request id)
  install_handlers  map typed errors from Components 2-4 and the framework to HTTP
  ErrorOut          the error body as an OpenAPI schema

Every failure, including framework validation errors and unknown routes, has the
same body: {"error": {"code", "message", "request_id"}}. Messages name the field or
rule, never a value. Unexpected exceptions become a 500 with the request id and
nothing else; only the exception *type* is logged (a library error such as a pydantic
ValidationError can contain the offending input, secrets included).
"""
import logging

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from sqlalchemy.exc import OperationalError
from starlette.exceptions import HTTPException as StarletteHTTPException

from core.connectors import ConfigInvalid, UnknownConnectorType
from core.engines import EngineDeclarationInvalid, UnknownEngine
from core.metrics import MetricError, ParameterInvalid, TemplateInvalid
from core.services.errors import AlreadyExists, IdempotencyConflict, NotFound

log = logging.getLogger("statloom.api")


class ApiError(Exception):
    def __init__(self, status: int, code: str, message: str):
        super().__init__(message)
        self.status, self.code, self.message = status, code, message


class ErrorBody(BaseModel):
    code: str
    message: str
    request_id: str


class ErrorOut(BaseModel):
    error: ErrorBody


# First match wins. Typed errors from the other components already carry value-free messages.
_TYPED: list[tuple[type[Exception], int, str]] = [
    (TemplateInvalid, 422, "template_invalid"),
    (ParameterInvalid, 422, "parameter_invalid"),
    (MetricError, 422, "metric_invalid"),
    (UnknownEngine, 422, "engine_invalid"),
    (EngineDeclarationInvalid, 422, "engine_invalid"),
    (NotFound, 404, "not_found"),
    (AlreadyExists, 409, "already_exists"),
    (IdempotencyConflict, 409, "idempotency_conflict"),
    (ConfigInvalid, 422, "connection_config_invalid"),
    (UnknownConnectorType, 422, "connection_config_invalid"),
]


def error_response(request: Request, status: int, code: str, message: str) -> JSONResponse:
    rid = getattr(request.state, "request_id", "unknown")
    return JSONResponse({"error": {"code": code, "message": message, "request_id": rid}},
                        status_code=status, headers={"X-Request-ID": rid})


def _field_problems(exc: RequestValidationError) -> str:
    """Field paths and error types only: pydantic's `input` and `ctx` can hold the value."""
    return "; ".join(f"{'.'.join(str(p) for p in e['loc'])}: {e['type']}" for e in exc.errors())


def install_handlers(app: FastAPI) -> None:
    @app.exception_handler(ApiError)
    async def _api_error(request: Request, exc: ApiError):
        return error_response(request, exc.status, exc.code, exc.message)

    @app.exception_handler(RequestValidationError)
    async def _invalid(request: Request, exc: RequestValidationError):
        return error_response(request, 422, "request_invalid", _field_problems(exc))

    @app.exception_handler(StarletteHTTPException)
    async def _http(request: Request, exc: StarletteHTTPException):
        code = {404: "not_found", 405: "method_not_allowed"}.get(exc.status_code, "http_error")
        return error_response(request, exc.status_code, code, str(exc.detail))

    def _typed(request: Request, exc: Exception):
        for cls, status, code in _TYPED:
            if isinstance(exc, cls):
                return error_response(request, status, code, str(exc))
        raise exc  # pragma: no cover  (handlers are only registered for the classes above)

    for cls, _status, _code in _TYPED:
        app.add_exception_handler(cls, _typed)

    @app.exception_handler(OperationalError)
    async def _database_down(request: Request, exc: OperationalError):
        log.error("database unavailable rid=%s", getattr(request.state, "request_id", "?"))
        return error_response(request, 503, "service_unavailable", "database unavailable")

    @app.exception_handler(Exception)
    async def _unexpected(request: Request, exc: Exception):
        log.error("unhandled %s rid=%s", type(exc).__name__, getattr(request.state, "request_id", "?"))
        return error_response(request, 500, "internal_error", "internal error")
