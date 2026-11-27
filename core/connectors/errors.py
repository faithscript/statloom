"""core/connectors/errors.py: typed connector failures, classified for retry decisions.

Navigation:
  ConnectorError           base class; `transient` says whether a retry may succeed
  ConnectionFailed         network / server unavailable            (transient)
  QueryTimeout             statement exceeded its time budget      (transient)
  AuthenticationFailed     bad credentials                         (permanent)
  QueryRejected            bad SQL, missing param, write attempt   (permanent)
  ResultTooLarge           result exceeded max_rows                (permanent)
  ConfigInvalid            connection config failed validation     (permanent)
  UnknownConnectorType     no provider registered for that type    (permanent)
  redact                   scrub a secret out of any message

Invariants: failures are typed and classified (callers never parse driver text),
and credentials never appear in an exception message.
"""


class ConnectorError(Exception):
    """Base class for every connector failure."""

    transient: bool = False  # True: a retry may succeed. False: retrying is pointless.

    def __init__(self, message: str = ""):
        super().__init__(message)
        self.message = message


class ConnectionFailed(ConnectorError):
    transient = True


class QueryTimeout(ConnectorError):
    transient = True


class AuthenticationFailed(ConnectorError):
    pass


class QueryRejected(ConnectorError):
    pass


class ResultTooLarge(ConnectorError):
    """Raised instead of truncating: partial rows would silently skew statistics."""


class ConfigInvalid(ConnectorError):
    pass


class UnknownConnectorType(ConnectorError):
    pass


def redact(text: str, secret: str) -> str:
    """Replace every occurrence of `secret` in `text` with ***. No-op for empty secrets."""
    return text.replace(secret, "***") if secret else text
