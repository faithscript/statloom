"""core/services: the only way callers (API, worker) touch the database model.

Services receive a session from `core.db.session_scope` and never commit or create sessions.
"""
