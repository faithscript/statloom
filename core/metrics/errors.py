"""core/metrics/errors.py: metric failures. Every one is permanent.

Navigation:
  MetricError             base class
  TemplateInvalid         registration-time template problem
  ParameterInvalid        bad parameter at registration or at submission
  OutputContractViolation query columns don't match the declared contract

Retrying the same input cannot fix any of these. Messages name the parameter or
rule and never include the offending value (values may be sensitive).
"""


class MetricError(Exception):
    """Base class. Permanent: retrying the same input cannot succeed."""

    transient = False


class TemplateInvalid(MetricError):
    """Disallowed syntax, undeclared or unused parameter, or size limit."""


class ParameterInvalid(MetricError):
    """Unknown, missing, wrongly typed, or over-limit parameter; or a bad default."""


class OutputContractViolation(MetricError):
    """The query's columns do not match the metric's declared output contract."""
