"""core/services/errors.py: typed service failures. Messages never contain values or secrets.

Navigation:
  NotFound               a referenced row does not exist
  AlreadyExists          a unique name is already taken
  IdempotencyConflict    same idempotency key, different request
  StaleClaim             a worker's fence (claimed_by + attempt_count) no longer matches the job
  SecretsConfigError / SecretDecryptionError   re-exported from core.security

All are permanent for the caller's request (retrying the same call cannot succeed), except
StaleClaim, which means "you are no longer the owner: stop".
"""
from core.security import SecretDecryptionError, SecretsConfigError


class NotFound(LookupError):
    pass


class AlreadyExists(Exception):
    pass


class IdempotencyConflict(Exception):
    pass


class StaleClaim(Exception):
    pass


__all__ = ["NotFound", "AlreadyExists", "IdempotencyConflict", "StaleClaim",
           "SecretsConfigError", "SecretDecryptionError"]
