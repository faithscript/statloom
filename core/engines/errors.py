"""core/engines/errors.py: engine failures. Every one is permanent.

Navigation:
  EngineError                base class
  InputInvalid               query result (or engine parameter) breaks the input rules
  InsufficientData           a variant has too few observations
  DegenerateData             the statistic is undefined (no variance): raised, never NaN
  OutputInvalid              an engine's return value breaks the output rules
  EngineMismatch             loaded engine differs from the pinned one, or can't take the metric type
  EngineCrashed              an engine raised an unexpected exception
  UnknownEngine              name not in the registry
  EngineDeclarationInvalid   incomplete engine class or duplicate registration

The same input and code fail the same way, so retrying cannot help. Messages name
the rule and the variant, never the offending values.
"""


class EngineError(Exception):
    """Base class. Permanent: retrying the same input cannot succeed."""

    transient = False


class InputInvalid(EngineError):
    pass


class InsufficientData(EngineError):
    pass


class DegenerateData(EngineError):
    pass


class OutputInvalid(EngineError):
    pass


class EngineMismatch(EngineError):
    pass


class EngineCrashed(EngineError):
    pass


class UnknownEngine(EngineError):
    pass


class EngineDeclarationInvalid(EngineError):
    pass
