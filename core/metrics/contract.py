"""core/metrics/contract.py: the output contract of a metric query.

Navigation:
  MetricType        continuous | proportion
  REQUIRED_COLUMNS  columns every metric query must return
  OPTIONAL_COLUMNS  columns the engine may use if present
  check_columns     raise OutputContractViolation on a missing required column

Proposal, to confirm with Component 4 (which owns the engine input): `variant`,
`value`, optional `unit_id`. Values are not checked here; for example, that a
proportion is 0/1 is checked by the engine adapter.
"""
from enum import Enum
from typing import Iterable

from core.metrics.errors import OutputContractViolation


class MetricType(str, Enum):
    CONTINUOUS = "continuous"
    PROPORTION = "proportion"


REQUIRED_COLUMNS = ("variant", "value")
OPTIONAL_COLUMNS = ("unit_id",)


def check_columns(columns: Iterable[str]) -> None:
    """Invariant: a query that does not return the declared columns is a permanent error.
    Extra columns are allowed."""
    have = set(columns)
    missing = [c for c in REQUIRED_COLUMNS if c not in have]
    if missing:
        raise OutputContractViolation(f"query result is missing required column(s): {', '.join(missing)}")
