"""tests/fakes.py: an in-memory second provider.

Registered at runtime by tests (never imported by core) to prove two things:
a new provider needs no core edits, and the contract holds for more than Postgres.
Behaves like a real connector for the query `... :n ...`: returns rows 1..n.
"""
from pydantic import BaseModel, ConfigDict

from core.connectors.base import Connector, QueryResult
from core.connectors.errors import QueryRejected, ResultTooLarge


class FakeConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    label: str = "fake"


class FakeConnector(Connector):
    config_model = FakeConfig

    def __init__(self, spec):
        super().__init__(spec)
        self._open = False
        self.close_calls = 0

    @property
    def is_open(self) -> bool:
        return self._open

    def open(self) -> None:
        self._open = True

    def close(self) -> None:
        self.close_calls += 1
        self._open = False

    def run_query(self, sql, params=None, *, timeout_s=30.0, max_rows=100_000):
        if not self._open:
            raise QueryRejected("connector is not open")
        if not params or "n" not in params:
            raise QueryRejected("missing query parameters: ['n']")
        n = params["n"]
        if n > max_rows:
            raise ResultTooLarge(f"query returned more than {max_rows} rows")
        return QueryResult(columns=("n",), rows=tuple((i,) for i in range(1, n + 1)))

    def validate(self) -> None:
        pass
