"""core/api: the REST API (Component 5).

Public surface: `create_app(ApiDeps(...))`. The API validates requests, records jobs, and
publishes job ids; it never runs an analysis, runs SQL, or connects to a user's database.
Phase A (built): errors, schemas, publisher interface, app factory, health, and the
stateless metric validate endpoint. Phase B (registration, jobs) waits for Component 1.
"""
from core.api.app import ApiDeps, create_app
from core.api.errors import ApiError
from core.api.publisher import JobPublisher, PublishFailed

__all__ = ["create_app", "ApiDeps", "ApiError", "JobPublisher", "PublishFailed"]
