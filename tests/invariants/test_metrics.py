"""Invariant tests for core/metrics (Component 3). Pure unit tests, plus one Postgres handoff test.

Navigation (one group per invariant):
  bound parameters only     hostile values never reach SQL text
  sandbox and allow-list    disallowed syntax rejected at registration
  declared <=> used         placeholder / if-variable / spec cross-check
  placeholder scanning      literals, comments, dollar quotes, casts
  parameter validation      types, limits, defaults, names; no value echo
  coercion                  dates, timezone-aware datetimes, lists
  determinism               identical output; stable content_hash
  optional blocks           `is not none` includes or drops block and param
  output contract           required columns
  independence              core/metrics imports no db / models / services / provider
  handoff to the connector  rendered query runs on real Postgres (the one DB-backed test)
The registry service (core/services/metric_registry.py) waits for Component 1's models.
"""
import ast
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

from core.connectors.sqlscan import find_placeholders
from core.metrics import (MetricType, OutputContractViolation, ParameterInvalid, ParamSpec,
                          ParamType, TemplateInvalid, check_columns, content_hash,
                          normalize_params, render, validate_template)

S = lambda name, type_=ParamType.STRING, **kw: ParamSpec(name=name, type=type_, **kw)  # noqa: E731
HOSTILE = ["'; DROP TABLE t; --", '" OR 1=1', "%s", ":other", "{{ 7*7 }}", "{% if x %}"]


# --- bound parameters only ---------------------------------------------
@pytest.mark.parametrize("hostile", HOSTILE)
def test_hostile_values_never_reach_sql_text(hostile):
    sql = "SELECT variant, value FROM t WHERE seg = :seg"
    benign = render(sql, [S("seg")], {"seg": "web"})
    bad = render(sql, [S("seg")], {"seg": hostile})
    assert bad.sql == benign.sql                      # byte-identical text
    assert bad.params == {"seg": hostile}             # value only in the bind dict
    assert hostile not in bad.sql


def test_text_changes_only_when_an_if_block_flips():
    sql = "SELECT 1 FROM t {% if seg is not none %}WHERE seg = :seg{% endif %}"
    specs = [S("seg", required=False)]
    a, b = render(sql, specs, {"seg": "x"}), render(sql, specs, {"seg": "'; DROP"})
    assert a.sql == b.sql
    assert render(sql, specs, {}).sql != a.sql


# --- sandbox and allow-list -------------------------------------------
@pytest.mark.parametrize("body", [
    "{{ x }}", "{{ ''.__class__ }}", "{% for i in x %}a{% endfor %}", "{% include 'a' %}",
    "{% import 'a' as b %}", "{% macro m() %}a{% endmacro %}", "{% set y = 1 %}",
    "{% if x|length > 1 %}a{% endif %}", "{% if x() %}a{% endif %}",
    "{% if x.attr %}a{% endif %}", "{% if x['k'] %}a{% endif %}", "{{ cycler }}",
    "{% if cycler %}a{% endif %}", "{% if x is divisibleby 3 %}a{% endif %}",
    "{# hidden #}", "{% if x == y %}a{% endif %}", "{% if x %}", "{% if x == [1] %}a{% endif %}",
])
def test_disallowed_syntax_rejected(body):
    with pytest.raises(TemplateInvalid):
        validate_template(f"SELECT :x {body}", [S("x")])


def test_oversized_template_rejected():
    with pytest.raises(TemplateInvalid):
        validate_template("SELECT :x " + "-" * 70_000, [S("x")])


def test_allowed_constructs_accepted():
    validate_template(
        "SELECT :x {% if x is defined and not (x is none) or x == 'a' %}a{% elif x >= 3 %}b"
        "{% else %}c{% endif %}", [S("x")])


# --- declared <=> used --------------------------------------------------
def test_undeclared_placeholder_rejected():
    with pytest.raises(TemplateInvalid, match="undeclared.*y"):
        validate_template("SELECT :x, :y", [S("x")])


def test_undeclared_if_variable_rejected():
    with pytest.raises(TemplateInvalid, match="undeclared.*z"):
        validate_template("SELECT :x {% if z %}1{% endif %}", [S("x")])


def test_declared_but_unused_rejected():
    with pytest.raises(TemplateInvalid, match="never used.*y"):
        validate_template("SELECT :x", [S("x"), S("y")])


def test_param_used_only_in_one_branch_counts():
    validate_template("SELECT 1 {% if a is not none %}WHERE v = :b{% endif %}",
                      [S("a", required=False), S("b", required=False)])


# --- placeholder scanning ----------------------------------------------
@pytest.mark.parametrize("sql", [
    "SELECT 'a:b'", 'SELECT "a:b"', "SELECT 1 -- :c\n", "SELECT 1 /* :c */",
    "SELECT $$ :c $$", "SELECT $t$ :c $t$", "SELECT x::int", "SELECT 'it''s :c'",
])
def test_colon_in_literal_comment_or_cast_is_not_a_placeholder(sql):
    assert find_placeholders(sql) == []


def test_real_placeholders_found_in_order_with_repeats():
    assert find_placeholders("SELECT :a, x::int, :b, :a") == ["a", "b", "a"]


# --- parameter validation ----------------------------------------------
def test_unknown_missing_and_wrong_type():
    specs = [S("n", ParamType.INTEGER)]
    for values in ({"n": 1, "extra": 1}, {}, {"n": "1"}, {"n": True}, {"n": 1.5}):
        with pytest.raises(ParameterInvalid):
            normalize_params(specs, values)


def test_over_limit_string_and_list():
    with pytest.raises(ParameterInvalid):
        normalize_params([S("s", max_length=3)], {"s": "abcd"})
    with pytest.raises(ParameterInvalid):
        normalize_params([S("l", ParamType.LIST, element_type=ParamType.INTEGER, max_length=2)],
                         {"l": [1, 2, 3]})


@pytest.mark.parametrize("specs", [
    [S("d", ParamType.INTEGER, default="x")],                        # bad default
    [S("a"), S("a")],                                                # duplicate
    [S("1bad")], [S("has space")], [S("x; DROP")],                   # invalid names
    [S("l", ParamType.LIST)],                                        # list w/o element type
    [S("l", ParamType.LIST, element_type=ParamType.LIST)],           # nested list
    [S("s", element_type=ParamType.STRING)],                         # element_type on non-list
    [S("i", ParamType.INTEGER, max_length=3)],                       # max_length on non-string
    [S(f"p{i}") for i in range(51)],                                 # too many
])
def test_bad_specs_rejected(specs):
    with pytest.raises(ParameterInvalid):
        validate_template("SELECT 1", specs)


def test_error_messages_never_contain_the_value():
    secret = "s3cr3t-value-XYZ"
    cases = [
        ([S("n", ParamType.INTEGER)], {"n": secret}),
        ([S("s", max_length=3)], {"s": secret}),
        ([S("d", ParamType.DATE)], {"d": secret}),
        ([S("t", ParamType.DATETIME)], {"t": secret}),
        ([S("l", ParamType.LIST, element_type=ParamType.INTEGER)], {"l": [secret]}),
    ]
    for specs, values in cases:
        with pytest.raises(ParameterInvalid) as e:
            normalize_params(specs, values)
        assert secret not in str(e.value)
    with pytest.raises(ParameterInvalid) as e:
        normalize_params([S("n")], {secret: 1})
    # an unknown *name* is echoed (it is not a value), but the value never is
    assert "1" not in str(e.value).replace("unknown parameter", "")


# --- coercion ------------------------------------------------------------
def test_date_coercion():
    out = normalize_params([S("d", ParamType.DATE)], {"d": "2024-03-01"})
    assert out["d"] == date(2024, 3, 1) and type(out["d"]) is date
    with pytest.raises(ParameterInvalid):  # a datetime is not a date
        normalize_params([S("d", ParamType.DATE)], {"d": datetime(2024, 3, 1, tzinfo=timezone.utc)})


def test_datetime_must_be_aware_and_becomes_utc():
    spec = [S("t", ParamType.DATETIME)]
    with pytest.raises(ParameterInvalid):
        normalize_params(spec, {"t": datetime(2024, 1, 1)})
    with pytest.raises(ParameterInvalid):
        normalize_params(spec, {"t": "2024-01-01T00:00:00"})
    est = timezone(timedelta(hours=-5))
    out = normalize_params(spec, {"t": datetime(2024, 1, 1, 12, tzinfo=est)})["t"]
    assert out == datetime(2024, 1, 1, 17, tzinfo=timezone.utc) and out.utcoffset() == timedelta(0)


def test_list_element_types_enforced():
    spec = [S("l", ParamType.LIST, element_type=ParamType.INTEGER)]
    assert normalize_params(spec, {"l": [1, 2]}) == {"l": [1, 2]}
    for bad in ([1, "2"], [True], "12"):
        with pytest.raises(ParameterInvalid):
            normalize_params(spec, {"l": bad})


def test_defaults_fill_and_optional_without_default_is_none():
    specs = [S("a", ParamType.INTEGER, required=False, default=5), S("b", required=False)]
    assert normalize_params(specs, {}) == {"a": 5, "b": None}


# --- determinism ---------------------------------------------------------
def test_render_is_deterministic_and_key_order_independent():
    sql = "SELECT :b, :a {% if c is not none %}AND z = :c{% endif %}"
    specs = [S("a"), S("b"), S("c", required=False)]
    first = render(sql, specs, {"a": "1", "b": "2", "c": "3"})
    for _ in range(100):
        again = render(sql, specs, {"c": "3", "b": "2", "a": "1"})
        assert again == first and list(again.params) == list(first.params) == ["a", "b", "c"]


def test_content_hash_stable_and_sensitive():
    sql, specs, t = "SELECT :a", [S("a")], MetricType.CONTINUOUS
    h = content_hash(sql, specs, t)
    assert h == content_hash(sql, list(specs), t) and len(h) == 64
    assert h != content_hash(sql + " ", specs, t)
    assert h != content_hash(sql, [S("a", required=False)], t)
    assert h != content_hash(sql, [S("a", ParamType.INTEGER)], t)
    assert h != content_hash(sql, specs, MetricType.PROPORTION)
    two = [S("a"), S("b")]
    assert content_hash(sql, two, t) == content_hash(sql, two[::-1], t)


# --- optional blocks -----------------------------------------------------
def test_is_not_none_includes_or_drops_block_and_param():
    sql = "SELECT 1 FROM t WHERE d >= :d {% if seg is not none %}AND seg = :seg{% endif %}"
    specs = [S("d", ParamType.DATE), S("seg", required=False)]
    with_seg = render(sql, specs, {"d": "2024-01-01", "seg": "x"})
    without = render(sql, specs, {"d": "2024-01-01"})
    assert ":seg" in with_seg.sql and set(with_seg.params) == {"d", "seg"}
    assert ":seg" not in without.sql and set(without.params) == {"d"}


def test_is_not_none_keeps_falsy_values_but_bare_truthiness_drops_them():
    specs = [S("n", ParamType.INTEGER, required=False)]
    good = "SELECT 1 {% if n is not none %}WHERE n = :n{% endif %}"
    trap = "SELECT 1 {% if n %}WHERE n = :n{% endif %}"
    assert ":n" in render(good, specs, {"n": 0}).sql      # 0 is a real value
    assert ":n" not in render(trap, specs, {"n": 0}).sql  # the trap: 0 is falsy


def test_render_rejects_invalid_template_itself():
    with pytest.raises(TemplateInvalid):
        render("SELECT {{ x }}", [S("x")], {"x": "1"})


# --- output contract -----------------------------------------------------
def test_output_contract():
    check_columns(["variant", "value"])
    check_columns(["unit_id", "extra", "value", "variant"])
    for cols in (["value"], ["variant"], [], ["VARIANT", "VALUE"]):
        with pytest.raises(OutputContractViolation):
            check_columns(cols)


# --- independence ----------------------------------------------------------
def test_core_metrics_imports_no_db_models_services_or_provider():
    forbidden = ("core.db", "core.models", "core.services", "core.connectors.postgres")
    root = Path(__file__).resolve().parents[2] / "core" / "metrics"
    for path in root.glob("*.py"):
        for node in ast.walk(ast.parse(path.read_text())):
            mods = ([a.name for a in node.names] if isinstance(node, ast.Import)
                    else [node.module or ""] if isinstance(node, ast.ImportFrom) else [])
            for m in mods:
                assert not m.startswith(forbidden), f"{path.name} imports {m}"


# --- handoff to the connector (real Postgres) -----------------------------
def test_rendered_query_runs_through_the_connector(user_db):
    """render() output is directly runnable by Component 2: list, optional block, and a
    hostile value all travel as binds; the contract check sees the real columns."""
    from core.connectors import ConnectionSpec, create_connector

    sql = ("SELECT name AS variant, id AS value FROM t WHERE id = ANY(:ids) "
           "{% if nm is not none %}AND name = :nm{% endif %} ORDER BY id")
    specs = [S("ids", ParamType.LIST, element_type=ParamType.INTEGER), S("nm", required=False)]
    spec = ConnectionSpec(type="postgres", secret=user_db["password"],
                          config={"host": user_db["host"], "port": user_db["port"],
                                  "database": user_db["database"], "user": user_db["user"]})
    with create_connector(spec) as c:
        q = render(sql, specs, {"ids": [1, 2, 3]})
        r = c.run_query(q.sql, q.params)
        check_columns(r.columns)
        assert [row[0] for row in r.rows] == ["alice", "bob", "'; DROP TABLE t; --"]
        q = render(sql, specs, {"ids": [1, 2, 3], "nm": "' OR 1=1 --"})
        assert len(c.run_query(q.sql, q.params).rows) == 0           # hostile value matched nothing
        assert len(c.run_query("SELECT id FROM t", {}).rows) == 3  # table intact
