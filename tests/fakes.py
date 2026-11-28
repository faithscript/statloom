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


# --- fake engines (Component 4) ---------------------------------------------
# A simple correct engine, plus a factory for deliberately bad ones. Registered at
# runtime by tests only (never imported by core), like FakeConnector above.
import math  # noqa: E402

from core.engines import (CONTRACT_VERSION, Comparison, Engine, EngineInput,  # noqa: E402
                          EngineOutput)
from core.metrics.contract import MetricType  # noqa: E402


class FakeEngineParams(BaseModel):
    model_config = ConfigDict(extra="forbid")


def mean_difference(inp: EngineInput, **overrides) -> EngineOutput:
    """A deliberately naive but valid result: difference of means, crude SE, fixed p."""
    out = []
    c = inp.samples[inp.control_variant]
    for variant, xs in inp.samples.items():
        if variant == inp.control_variant:
            continue
        mc, mt = sum(c) / len(c), sum(xs) / len(xs)
        se = 1.0 / math.sqrt(len(c) + len(xs))
        fields = dict(control=inp.control_variant, treatment=variant, n_control=len(c),
                      n_treatment=len(xs), mean_control=mc, mean_treatment=mt, effect=mt - mc,
                      effect_se=se, ci_lower=mt - mc - 2 * se, ci_upper=mt - mc + 2 * se,
                      confidence_level=0.95, p_value=0.5, method="fake_mean_difference")
        fields.update(overrides)
        out.append(Comparison.model_construct(**fields) if overrides else Comparison(**fields))
    return EngineOutput.model_construct(contract_version=CONTRACT_VERSION, comparisons=tuple(out)) \
        if overrides else EngineOutput(contract_version=CONTRACT_VERSION, comparisons=tuple(out))


class FakeEngine(Engine):
    name = "fake_mean_difference"
    version = "0.1"
    supports = frozenset({MetricType.CONTINUOUS, MetricType.PROPORTION})
    params_model = FakeEngineParams

    def run(self, inp: EngineInput) -> EngineOutput:
        return mean_difference(inp)


def bad_engine(label: str, produce) -> type[Engine]:
    """An engine class whose run() returns `produce(inp)` (any value, valid or not)."""
    return type(f"Bad_{label}", (Engine,), {
        "name": f"bad_{label}", "version": "0.1",
        "supports": frozenset({MetricType.CONTINUOUS, MetricType.PROPORTION}),
        "params_model": FakeEngineParams, "run": lambda self, inp: produce(inp)})
