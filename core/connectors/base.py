"""core/connectors/base.py: the provider-neutral connector interface.

Navigation:
  ConnectionSpec   type + config + masked secret (what core code hands to a connector)
  QueryResult      uniform, immutable result shape returned by every provider
  Connector        abstract base: open / close / run_query / validate, context manager

Core job code depends on THIS module and registry.py only, never on a provider.
This module must not import core.db, core.models, or core.services: connectors
receive a plain ConnectionSpec and know nothing about Statloom's persistence.
"""
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any, ClassVar, Mapping

from pydantic import BaseModel, ConfigDict, SecretStr, ValidationError

from core.connectors.errors import ConfigInvalid, redact


class ConnectionSpec(BaseModel):
    """Everything a connector needs to connect.

    Invariant: credentials stay secret. `secret` is a SecretStr, so repr/str/logging
    show '**********'; the raw value is only read inside a connector at connect time.
    """

    model_config = ConfigDict(frozen=True)

    type: str
    config: dict[str, Any]
    secret: SecretStr


@dataclass(frozen=True)
class QueryResult:
    """Invariant: uniform results. Every provider returns exactly this shape."""

    columns: tuple[str, ...]
    rows: tuple[tuple[Any, ...], ...]

    @property
    def row_count(self) -> int:
        return len(self.rows)


class Connector(ABC):
    """Interface every provider implements. Subclasses set `config_model`.

    Lifecycle: construct (validates config, does not connect) -> open() -> run_query()
    any number of times -> close(). Prefer `with connector:` so close() always runs.
    """

    config_model: ClassVar[type[BaseModel]]

    def __init__(self, spec: ConnectionSpec):
        self.spec = spec
        self.config = self._parse_config(spec)

    def _parse_config(self, spec: ConnectionSpec) -> BaseModel:
        """Invariant: config is validated before use; unknown fields are rejected."""
        try:
            return self.config_model.model_validate(spec.config)
        except ValidationError as exc:
            # Only field names and messages, never the offending input values.
            detail = "; ".join(
                f"{'.'.join(str(p) for p in e['loc']) or 'config'}: {e['msg']}"
                for e in exc.errors()
            )
            raise ConfigInvalid(redact(detail, spec.secret.get_secret_value())) from None

    # --- lifecycle -------------------------------------------------------
    @abstractmethod
    def open(self) -> None:
        """Establish the connection. Raises a ConnectorError on failure."""

    @abstractmethod
    def close(self) -> None:
        """Release the connection. MUST be idempotent and never raise."""

    @property
    @abstractmethod
    def is_open(self) -> bool: ...

    def __enter__(self) -> "Connector":
        self.open()
        return self

    def __exit__(self, *exc_info) -> None:
        # Invariant: no leaked connections: closed on success, error, and timeout.
        self.close()

    # --- work ------------------------------------------------------------
    @abstractmethod
    def run_query(
        self,
        sql: str,
        params: Mapping[str, Any] | None = None,
        *,
        timeout_s: float = 30.0,
        max_rows: int = 100_000,
    ) -> QueryResult:
        """Run ONE read-only query. SQL uses `:name` placeholders; values arrive only
        through `params` (invariant: bound parameters only, never interpolated).

        Raises QueryTimeout past `timeout_s`, ResultTooLarge past `max_rows`
        (never a silent partial result), QueryRejected for bad or non-read-only SQL.
        """

    @abstractmethod
    def validate(self) -> None:
        """Connect, confirm the session is read-only, run a trivial query.
        Returns normally on success; raises a ConnectorError otherwise."""
