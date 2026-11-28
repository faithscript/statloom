"""core/api/app.py: build the FastAPI app from injected dependencies.

Navigation:
  ApiDeps      session factory (optional), publisher (optional), body limit
  create_app   wire routes, handlers, and the request guard

The guard (one middleware) gives every request an id, bounds the body, and writes an
access log of method, path, status, and duration only: never bodies, query strings, or
headers. Routes that need Postgres (connections, metric and engine registration, jobs) are
registered only when a session factory is given, so the stateless parts work (and are testable)
with no database or Kafka. Giving a session factory also validates the encryption key at startup:
a missing or malformed STATLOOM_SECRET_KEY raises SecretsConfigError from create_app, naming the
variable, never the key. Without a publisher, a submitted job is stored and stays `queued`.
"""
import logging
import re
import time
import uuid
from dataclasses import dataclass

from fastapi import FastAPI, Request
from sqlalchemy.orm import Session, sessionmaker

from core.api.errors import ErrorOut, error_response, install_handlers
from core.api.publisher import JobPublisher
from core.api.routes import connections, engines, health, jobs, metrics
from core.security import check_secret_key

access_log = logging.getLogger("statloom.api.access")
_REQUEST_ID = re.compile(r"[A-Za-z0-9._-]{1,64}\Z")
_BODY_METHODS = {"POST", "PUT", "PATCH"}


@dataclass(frozen=True)
class ApiDeps:
    session_factory: sessionmaker[Session] | None = None
    publisher: JobPublisher | None = None
    max_body_bytes: int = 1024 * 1024


def create_app(deps: ApiDeps | None = None) -> FastAPI:
    deps = deps or ApiDeps()
    if deps.session_factory is not None:
        check_secret_key()          # fail at startup, not on the first encrypt or decrypt
    app = FastAPI(title="Statloom API", version="0.1.0",
                  responses={422: {"model": ErrorOut}, 500: {"model": ErrorOut}})
    app.state.deps = deps
    install_handlers(app)

    @app.middleware("http")
    async def guard(request: Request, call_next):
        start = time.perf_counter()
        supplied = request.headers.get("x-request-id", "")
        # Accept a well-formed client id; anything else (e.g. header injection) is replaced.
        request.state.request_id = supplied if _REQUEST_ID.match(supplied) else uuid.uuid4().hex
        response = None
        if request.method in _BODY_METHODS:
            length = request.headers.get("content-length")
            if length is None or not length.isdigit():
                response = error_response(request, 411, "length_required",
                                          "a Content-Length header is required")
            elif int(length) > deps.max_body_bytes:
                response = error_response(request, 413, "request_too_large",
                                          f"request body exceeds {deps.max_body_bytes} bytes")
        if response is None:
            response = await call_next(request)
        response.headers["X-Request-ID"] = request.state.request_id
        access_log.info("%s %s %s %.1fms rid=%s", request.method, request.url.path,
                        response.status_code, (time.perf_counter() - start) * 1000,
                        request.state.request_id)
        return response

    app.include_router(health.make_router(deps))
    app.include_router(metrics.router)
    if deps.session_factory is not None:
        app.include_router(connections.router)
        app.include_router(metrics.registration_router)
        app.include_router(engines.router)
        app.include_router(jobs.router)
    return app
