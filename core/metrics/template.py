"""core/metrics/template.py: validate and render SQL metric templates.

Navigation:
  _env              the one sandboxed Jinja environment
  validate_template parse, allow-list AST walk, declared <=> used check
  RenderedQuery     (sql, params): the only thing handed to a connector
  render            resolve {% if %} blocks, return SQL with :name placeholders + binds
  content_hash      SHA-256 of template + spec + type (traceability)

A template is SQL text with `:name` placeholders for values and optional
`{% if %}` blocks. Nothing else is allowed: no `{{ }}` output, calls, attributes,
filters, loops, includes, macros, or `set`. Values can't reach SQL text because the
language has no way to output them; this is proved at registration, not filtered.
"""
import hashlib
import json
import re
from dataclasses import dataclass, field
from typing import Any, Mapping, Sequence

from jinja2 import StrictUndefined, nodes
from jinja2.exceptions import TemplateSyntaxError
from jinja2.sandbox import ImmutableSandboxedEnvironment

from core.connectors.sqlscan import find_placeholders
from core.metrics.contract import MetricType
from core.metrics.errors import TemplateInvalid
from core.metrics.parameters import ParamSpec, normalize_params, validate_specs

MAX_TEMPLATE_BYTES = 64 * 1024
_ALLOWED_TESTS = {"none", "defined"}
_COMPARE_OPS = {"eq", "ne", "lt", "lteq", "gt", "gteq"}
_TAG = re.compile(r"\{%.*?%\}", re.DOTALL)


def _env() -> ImmutableSandboxedEnvironment:
    env = ImmutableSandboxedEnvironment(undefined=StrictUndefined, autoescape=False,
                                        keep_trailing_newline=True)
    env.globals = {}  # no range, dict, cycler, ...
    return env


def _walk(node: nodes.Node, variables: set[str]) -> None:
    """Allow-list walk. Anything not listed here is rejected."""
    if isinstance(node, nodes.Template):
        for child in node.body:
            _walk(child, variables)
    elif isinstance(node, nodes.Output):
        for child in node.nodes:
            if not isinstance(child, nodes.TemplateData):
                raise TemplateInvalid("value output ({{ ... }}) is not allowed; use :name placeholders")
    elif isinstance(node, nodes.If):
        _walk(node.test, variables)
        for child in (*node.body, *node.elif_, *node.else_):
            _walk(child, variables)
    elif isinstance(node, nodes.Name):
        if node.ctx != "load":
            raise TemplateInvalid("assignment is not allowed")
        variables.add(node.name)
    elif isinstance(node, (nodes.And, nodes.Or)):
        _walk(node.left, variables)
        _walk(node.right, variables)
    elif isinstance(node, nodes.Not):
        _walk(node.node, variables)
    elif isinstance(node, nodes.Test):
        if node.name not in _ALLOWED_TESTS or node.args or node.kwargs or node.dyn_args or node.dyn_kwargs:
            raise TemplateInvalid(f"test '{node.name}' is not allowed (only 'is none' and 'is defined')")
        _walk(node.node, variables)
    elif isinstance(node, nodes.Compare):
        if not isinstance(node.expr, nodes.Name):
            raise TemplateInvalid("comparisons must have a parameter on the left")
        _walk(node.expr, variables)
        for op in node.ops:
            if op.op not in _COMPARE_OPS or not isinstance(op.expr, nodes.Const):
                raise TemplateInvalid("comparisons must be against literals")
            if not (op.expr.value is None or isinstance(op.expr.value, (str, int, float, bool))):
                raise TemplateInvalid("comparisons must be against simple literals")
    else:
        raise TemplateInvalid(f"template syntax '{type(node).__name__}' is not allowed")


def validate_template(sql: str, specs: Sequence[ParamSpec]) -> None:
    """Registration-time check. Invariants: no code execution; every placeholder and
    `if` variable is declared and every declared parameter is used; bounded size."""
    validate_specs(specs)
    if len(sql.encode("utf-8")) > MAX_TEMPLATE_BYTES:
        raise TemplateInvalid(f"template exceeds {MAX_TEMPLATE_BYTES} bytes")
    if "{#" in sql:
        raise TemplateInvalid("template comments ({# ... #}) are not allowed")
    try:
        ast = _env().parse(sql)
    except TemplateSyntaxError as e:
        raise TemplateInvalid(f"template syntax error on line {e.lineno}") from None
    variables: set[str] = set()
    _walk(ast, variables)
    # Tags are stripped from the raw text so every branch's placeholders count.
    used = set(find_placeholders(_TAG.sub(" ", sql))) | variables
    declared = {s.name for s in specs}
    undeclared = sorted(used - declared)
    if undeclared:
        raise TemplateInvalid(f"undeclared parameter(s) used: {', '.join(undeclared)}")
    unused = sorted(declared - used)
    if unused:
        raise TemplateInvalid(f"declared parameter(s) never used: {', '.join(unused)}")


@dataclass(frozen=True)
class RenderedQuery:
    """Invariant: values live only in `params`, never in `sql`."""

    sql: str
    params: dict[str, Any] = field(default_factory=dict)


def render(sql: str, specs: Sequence[ParamSpec], values: Mapping[str, Any]) -> RenderedQuery:
    """Validate, normalize values, resolve `{% if %}` blocks, keep only the params
    whose placeholders survive. Deterministic: same input, identical output."""
    validate_template(sql, specs)
    normalized = normalize_params(specs, values)
    text = _env().from_string(sql).render(**normalized)
    surviving = set(find_placeholders(text))
    return RenderedQuery(sql=text, params={k: v for k, v in normalized.items() if k in surviving})


def content_hash(sql: str, specs: Sequence[ParamSpec], metric_type: MetricType) -> str:
    """SHA-256 over a canonical JSON encoding of template, specs (sorted by name), and type."""
    payload = {
        "sql": sql,
        "specs": [s.model_dump(mode="json") for s in sorted(specs, key=lambda s: s.name)],
        "metric_type": MetricType(metric_type).value,
    }
    blob = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(blob.encode("ascii")).hexdigest()
