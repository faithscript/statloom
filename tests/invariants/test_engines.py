"""Invariant tests for core/engines (Component 4). Pure unit tests, plus one Postgres handoff test.

Navigation (one group per invariant):
  statistical correctness   reference engine vs hand calculations and SciPy
  properties                symmetry, bracketing, shift invariance, level monotonicity
  calibration               seeded simulations: type-I error and CI coverage
  input validation          bad rows fail loudly, messages never contain values
  degenerate data           undefined statistics raise, never NaN
  output validation         malformed engine output rejected
  crash handling            an engine exception becomes a typed, data-free error
  pinned version            wrong name / version / contract / metric type refused
  registry and plug-in      a new engine needs no core edit; incomplete ones rejected
  contract suite            same checks against a fake engine and the reference engine
  determinism, storable     repeatable; JSON round trip
  handoff                   Component 3 render -> real Postgres -> engine (the DB-backed test)
  independence              core/engines imports no db / models / services / provider
The registry service (core/services/engine_registry.py) waits for Component 1's models.
"""
import ast
import math
import random
from pathlib import Path

import numpy as np
import pytest
from scipy import stats

from core.engines import (CONTRACT_VERSION, DegenerateData, EngineCrashed,
                          EngineDeclarationInvalid, EngineMismatch, InputInvalid,
                          InsufficientData, OutputInvalid, PinnedEngine, UnknownEngine,
                          build_input, check_output, create_engine, register, registered_names,
                          run_engine, to_json)
from core.engines import registry as engine_registry
from core.engines.contract import EngineOutput
from core.engines.reference import ReferenceEngine, ReferenceParams, _two_prop, _welch
from core.metrics import MetricType
from tests.fakes import FakeEngine, FakeEngineParams, bad_engine, mean_difference

CONT, PROP = MetricType.CONTINUOUS, MetricType.PROPORTION
COLS = ("variant", "value")
REF_PIN = PinnedEngine("reference", ReferenceEngine.version)
PARAMS = ReferenceParams()


def rows_of(**groups):
    return [(v, x) for v, xs in groups.items() for x in xs]


def run_ref(rows, metric=CONT, control="c", params=None, cols=COLS):
    return run_engine(create_engine("reference"), REF_PIN, metric, cols, rows, control, params or {})


def only(output):
    assert len(output.comparisons) == 1
    return output.comparisons[0]


@pytest.fixture
def fake_registered():
    engine_registry.register("fake_mean_difference")(FakeEngine)
    yield
    engine_registry.unregister("fake_mean_difference")


# --- statistical correctness ---------------------------------------------
def test_continuous_hand_calculation():
    c = only(run_ref(rows_of(c=[1, 2, 3], t=[2, 4, 6])))
    assert c.effect == pytest.approx(2.0) and c.method == "welch_t_test"
    assert c.effect_se == pytest.approx(math.sqrt(5 / 3))        # sqrt(1/3 + 4/3)
    assert (c.n_control, c.n_treatment) == (3, 3)
    assert (c.mean_control, c.mean_treatment) == (2.0, 4.0)


@pytest.mark.parametrize("seed,nc,nt,sc,st", [(1, 30, 30, 1, 1), (2, 12, 80, 1, 5),
                                              (3, 200, 15, 3, 0.5), (4, 7, 9, 2, 2)])
def test_continuous_matches_scipy_welch(seed, nc, nt, sc, st):
    rng = np.random.default_rng(seed)
    xc, xt = rng.normal(10, sc, nc), rng.normal(10.7, st, nt)
    c = only(run_ref(rows_of(c=xc.tolist(), t=xt.tolist())))
    ref = stats.ttest_ind(xt, xc, equal_var=False)
    ci = ref.confidence_interval(0.95)
    assert c.p_value == pytest.approx(ref.pvalue, rel=1e-9)
    assert c.ci_lower == pytest.approx(ci.low, rel=1e-9) and c.ci_upper == pytest.approx(ci.high, rel=1e-9)
    assert c.effect == pytest.approx(xt.mean() - xc.mean())


def test_proportion_hand_calculation_and_chi_square_agreement():
    rows = rows_of(c=[1] * 20 + [0] * 80, t=[1] * 30 + [0] * 70)
    c = only(run_ref(rows, PROP))
    assert c.method == "two_proportion_z_test"
    assert (c.mean_control, c.mean_treatment, c.effect) == (0.2, 0.3, pytest.approx(0.1))
    se_unpooled = math.sqrt(0.3 * 0.7 / 100 + 0.2 * 0.8 / 100)
    assert c.effect_se == pytest.approx(se_unpooled)
    assert (c.ci_lower, c.ci_upper) == pytest.approx((0.1 - 1.959964 * se_unpooled,
                                                      0.1 + 1.959964 * se_unpooled), abs=1e-5)
    assert c.p_value == pytest.approx(0.10247, abs=1e-4)          # z = 0.1 / sqrt(.25*.75*.02)
    chi2 = stats.chi2_contingency([[20, 80], [30, 70]], correction=False)
    assert c.p_value == pytest.approx(chi2.pvalue, rel=1e-9)      # z^2 == chi-square, no correction


def test_booleans_accepted_for_proportions_only():
    only(run_ref([("c", True), ("c", False), ("t", True), ("t", True)], PROP))
    with pytest.raises(InputInvalid):
        run_ref([("c", True), ("c", False), ("t", True), ("t", True)], CONT)


def test_each_treatment_is_compared_to_the_control_separately():
    out = run_ref(rows_of(c=[1, 2, 3, 4], b=[2, 3, 4, 5], a=[5, 6, 7, 9]))
    assert [x.treatment for x in out.comparisons] == ["a", "b"]    # deterministic order
    assert all(x.control == "c" for x in out.comparisons)


# --- properties ------------------------------------------------------------
def _data(seed=5, n=25):
    rng = np.random.default_rng(seed)
    return rng.normal(0, 1, n).tolist(), rng.normal(0.4, 1.5, n + 3).tolist()


def test_swapping_control_and_treatment_negates_effect_and_keeps_p():
    xc, xt = _data()
    a = only(run_ref(rows_of(c=xc, t=xt)))
    b = only(run_ref(rows_of(c=xt, t=xc)))
    assert b.effect == pytest.approx(-a.effect)
    assert (b.ci_lower, b.ci_upper) == pytest.approx((-a.ci_upper, -a.ci_lower))
    assert b.p_value == pytest.approx(a.p_value) and b.effect_se == pytest.approx(a.effect_se)


def test_ci_brackets_effect_and_widens_with_confidence():
    xc, xt = _data()
    widths = []
    for level in (0.80, 0.95, 0.99):
        c = only(run_ref(rows_of(c=xc, t=xt), params={"confidence_level": level}))
        assert c.ci_lower <= c.effect <= c.ci_upper and 0 <= c.p_value <= 1
        widths.append(c.ci_upper - c.ci_lower)
    assert widths == sorted(widths) and len(set(widths)) == 3


def test_shifting_all_values_changes_nothing_but_the_means():
    xc, xt = _data()
    a = only(run_ref(rows_of(c=xc, t=xt)))
    b = only(run_ref(rows_of(c=[x + 1000 for x in xc], t=[x + 1000 for x in xt])))
    assert b.effect == pytest.approx(a.effect, abs=1e-9)
    assert b.p_value == pytest.approx(a.p_value, rel=1e-6) and b.effect_se == pytest.approx(a.effect_se, rel=1e-6)


# --- calibration (seeded: deterministic, not flaky) -----------------------------
def test_type_one_error_and_ci_coverage_under_the_null():
    rng = np.random.default_rng(20240607)
    sims, rejects, covers = 3000, 0, 0
    for _ in range(sims):
        xc, xt = rng.normal(5, 2, 25), rng.normal(5, 3, 40)       # true effect 0, unequal variances
        *_, lo, hi, p, _m = _welch(tuple(xc), tuple(xt), 0.95, ("c", "t"))
        rejects += p < 0.05
        covers += lo <= 0 <= hi
    assert abs(rejects / sims - 0.05) < 0.012
    assert abs(covers / sims - 0.95) < 0.012


def test_proportion_calibration():
    rng = np.random.default_rng(99)
    sims, rejects = 3000, 0
    for _ in range(sims):
        c = tuple(float(v) for v in rng.random(200) < 0.3)
        t = tuple(float(v) for v in rng.random(200) < 0.3)
        *_, p, _m = _two_prop(c, t, 0.95, ("c", "t"))
        rejects += p < 0.05
    assert abs(rejects / sims - 0.05) < 0.015


# --- input validation ---------------------------------------------------------
SECRET = "s3cr3t-value-XYZ"
GOOD = rows_of(c=[1.0, 2.0, 3.0], t=[2.0, 3.0, 4.0])


@pytest.mark.parametrize("rows,metric,exc", [
    ([("c", None)] + GOOD[1:], CONT, InputInvalid),                      # null value
    ([("c", float("nan"))] + GOOD[1:], CONT, InputInvalid),
    ([("c", float("inf"))] + GOOD[1:], CONT, InputInvalid),
    ([("c", SECRET)] + GOOD[1:], CONT, InputInvalid),                    # text is never coerced
    ([(None, 1.0)] + GOOD[1:], CONT, InputInvalid),                      # null variant
    ([("c", 2)] + [("c", 1), ("t", 1), ("t", 0)], PROP, InputInvalid),    # proportion of 2
    ([("c", 0.5)] + [("c", 1), ("t", 1), ("t", 0)], PROP, InputInvalid),  # proportion of 0.5
    (rows_of(t=[1, 2, 3], u=[2, 3, 4]), CONT, InputInvalid),             # control missing
    (rows_of(c=[1, 2, 3]), CONT, InputInvalid),                          # single variant
    (rows_of(c=[1, 2, 3], t=[5]), CONT, InsufficientData),               # one observation
])
def test_bad_input_raises_the_right_error_without_echoing_values(rows, metric, exc):
    with pytest.raises(exc) as e:
        build_input(metric, COLS, rows, "c", PARAMS)
    assert SECRET not in str(e.value)


def test_missing_required_column():
    with pytest.raises(InputInvalid, match="value"):
        build_input(CONT, ("variant",), [("c",)], "c", PARAMS)


def test_unit_id_rules():
    cols = ("variant", "value", "unit_id")
    ok = [("c", 1.0, 1), ("c", 2.0, 2), ("t", 3.0, 3), ("t", 4.0, 4)]
    build_input(CONT, cols, ok, "c", PARAMS)
    for bad in ([("c", 1.0, 1), ("c", 2.0, 1), ("t", 3.0, 3), ("t", 4.0, 4)],      # twice in a variant
                [("c", 1.0, 1), ("c", 2.0, 2), ("t", 3.0, 1), ("t", 4.0, 4)],      # in two variants
                [("c", 1.0, None), ("c", 2.0, 2), ("t", 3.0, 3), ("t", 4.0, 4)]):  # null unit
        with pytest.raises(InputInvalid):
            build_input(CONT, cols, bad, "c", PARAMS)


def test_bad_engine_parameters():
    for params in ({"confidence_level": 1.5}, {"confidence_level": "high"}, {"nope": 1}):
        with pytest.raises(InputInvalid, match="parameter"):
            run_ref(GOOD, params=params)


def test_decimal_values_from_postgres_numeric_are_accepted():
    from decimal import Decimal
    only(run_ref([("c", Decimal("1.5")), ("c", Decimal("2.5")), ("t", Decimal("3")), ("t", Decimal("4"))]))


# --- degenerate data ------------------------------------------------------------
@pytest.mark.parametrize("rows,metric", [
    (rows_of(c=[2, 2, 2], t=[2, 2, 2]), CONT),                         # no variance anywhere
    (rows_of(c=[0, 0, 0], t=[0, 0, 0]), PROP),                         # all 0
    (rows_of(c=[1, 1, 1], t=[1, 1, 1]), PROP),                         # all 1
])
def test_undefined_statistics_raise_instead_of_nan(rows, metric):
    with pytest.raises(DegenerateData):
        run_ref(rows, metric)


def test_one_constant_group_is_fine():
    c = only(run_ref(rows_of(c=[2, 2, 2], t=[1, 2, 3])))
    assert math.isfinite(c.p_value) and math.isfinite(c.effect_se)


# --- output validation -------------------------------------------------------------
def _run_bad(produce, rows=GOOD):
    return run_engine(bad_engine("x", produce)(), PinnedEngine("bad_x", "0.1"), CONT, COLS, rows, "c", {})


@pytest.mark.parametrize("label,overrides", [
    ("nan", {"effect": float("nan")}), ("inf", {"ci_upper": float("inf")}),
    ("negse", {"effect_se": -1.0}), ("nobracket", {"ci_lower": 99.0, "ci_upper": 100.0}),
    ("pbig", {"p_value": 1.5}), ("pneg", {"p_value": -0.1}), ("counts", {"n_treatment": 99}),
    ("level", {"confidence_level": 1.0}), ("control", {"control": "z"}),
])
def test_malformed_output_is_rejected(label, overrides):
    with pytest.raises(OutputInvalid):
        _run_bad(lambda inp: mean_difference(inp, **overrides))


def test_extra_field_smuggled_in_a_subclass_cannot_reach_storage():
    """The runner returns a re-validated copy, so only contract fields survive."""
    from core.engines import Comparison

    class Sneaky(Comparison):
        surprise: int = 1                      # not part of the contract

    def produce(inp):
        good = mean_difference(inp).comparisons[0]
        return EngineOutput.model_construct(contract_version=CONTRACT_VERSION,
                                            comparisons=(Sneaky(**good.model_dump()),))
    out = _run_bad(produce)
    assert type(out.comparisons[0]) is Comparison
    assert "surprise" not in to_json(out)["comparisons"][0]


@pytest.mark.parametrize("produce", [
    lambda inp: {"contract_version": 1},                       # wrong type
    lambda inp: None,
    lambda inp: EngineOutput.model_construct(contract_version=CONTRACT_VERSION, comparisons=()),
    lambda inp: EngineOutput.model_construct(contract_version=99, comparisons=mean_difference(inp).comparisons),
    lambda inp: EngineOutput.model_construct(                  # a treatment is missing
        contract_version=CONTRACT_VERSION, comparisons=mean_difference(inp).comparisons[:0] or ()),
])
def test_wrong_shape_or_missing_treatment_rejected(produce):
    with pytest.raises(OutputInvalid):
        _run_bad(produce)


def test_missing_treatment_among_several():
    rows = rows_of(c=[1, 2, 3], a=[2, 3, 4], b=[3, 4, 5])
    with pytest.raises(OutputInvalid, match="one comparison per treatment"):
        _run_bad(lambda inp: EngineOutput.model_construct(
            contract_version=CONTRACT_VERSION, comparisons=mean_difference(inp).comparisons[:1]), rows)


def test_output_errors_do_not_echo_values():
    with pytest.raises(OutputInvalid) as e:
        _run_bad(lambda inp: mean_difference(inp, p_value=123456.789))
    assert "123456" not in str(e.value)


# --- crash handling ------------------------------------------------------------------
def test_engine_exception_becomes_typed_error_without_the_text():
    def boom(inp):
        raise ValueError("secret-row-data")
    with pytest.raises(EngineCrashed) as e:
        _run_bad(boom)
    assert "secret-row-data" not in str(e.value) and "ValueError" in str(e.value)
    assert isinstance(e.value.__cause__, ValueError)             # original is chained for debugging


def test_all_engine_errors_are_permanent():
    from core.engines import EngineError
    assert EngineError.transient is False
    for exc in (InputInvalid, InsufficientData, DegenerateData, OutputInvalid, EngineMismatch,
                EngineCrashed, UnknownEngine, EngineDeclarationInvalid):
        assert issubclass(exc, EngineError)


# --- pinned version -----------------------------------------------------------------------
@pytest.mark.parametrize("pin", [PinnedEngine("other", "1.0.0"), PinnedEngine("reference", "9.9.9"),
                                 PinnedEngine("reference", "1.0.0", contract_version=2)])
def test_pin_mismatch_refused(pin):
    with pytest.raises(EngineMismatch):
        run_engine(create_engine("reference"), pin, CONT, COLS, GOOD, "c", {})


def test_unsupported_metric_type_refused():
    class OnlyContinuous(FakeEngine):
        name = "only_cont"
        supports = frozenset({CONT})
    with pytest.raises(EngineMismatch, match="proportion"):
        run_engine(OnlyContinuous(), PinnedEngine("only_cont", "0.1"), PROP, COLS,
                   rows_of(c=[0, 1], t=[1, 0]), "c", {})


# --- registry and plug-in -----------------------------------------------------------------------
def test_reference_engine_is_registered():
    assert "reference" in registered_names()


def test_new_engine_plugs_in_without_core_changes(fake_registered):
    assert "fake_mean_difference" in registered_names()
    out = run_engine(create_engine("fake_mean_difference"), PinnedEngine("fake_mean_difference", "0.1"),
                     CONT, COLS, GOOD, "c", {})
    assert only(out).method == "fake_mean_difference"


def test_registry_is_strict(fake_registered):
    with pytest.raises(EngineDeclarationInvalid, match="already"):
        register("fake_mean_difference")(FakeEngine)
    with pytest.raises(EngineDeclarationInvalid, match="registered as"):
        register("some_other_name")(FakeEngine)
    with pytest.raises(UnknownEngine):
        create_engine("nope")


@pytest.mark.parametrize("attrs", [
    {"version": ""}, {"name": ""}, {"supports": frozenset()}, {"supports": frozenset({"continuous"})},
    {"params_model": dict},
])
def test_incomplete_engine_declarations_rejected(attrs):
    cls = type("Incomplete", (FakeEngine,), {"name": "incomplete", **attrs})
    with pytest.raises(EngineDeclarationInvalid):
        register(cls.name)(cls)


def test_engine_without_run_rejected():
    cls = type("NoRun", (FakeEngine.__mro__[1],), {"name": "norun", "version": "1",
               "supports": frozenset({CONT}), "params_model": FakeEngineParams})
    with pytest.raises(EngineDeclarationInvalid):
        register("norun")(cls)


# --- contract suite: same checks against the fake and the reference engine -----------------------
@pytest.fixture(params=["reference", "fake_mean_difference"])
def any_engine(request, fake_registered):
    eng = create_engine(request.param)
    return eng, PinnedEngine(eng.name, eng.version)


def test_contract_same_shapes_in_and_out(any_engine):
    eng, pin = any_engine
    out = run_engine(eng, pin, CONT, COLS, rows_of(c=[1, 2, 3, 5], a=[2, 3, 4, 8], b=[4, 5, 7, 9]), "c", {})
    assert isinstance(out, EngineOutput) and out.contract_version == CONTRACT_VERSION
    assert [c.treatment for c in out.comparisons] == ["a", "b"]


def test_contract_deterministic_and_json_round_trip(any_engine):
    import json
    eng, pin = any_engine
    rows = rows_of(c=[1, 2, 3, 5], t=[2, 3, 4, 8])
    first = run_engine(eng, pin, CONT, COLS, rows, "c", {})
    for _ in range(100):
        assert run_engine(eng, pin, CONT, COLS, rows, "c", {}) == first
    assert EngineOutput.model_validate(json.loads(json.dumps(to_json(first)))) == first


def test_contract_engine_cannot_see_connectors_or_secrets(any_engine):
    eng, _ = any_engine
    import inspect
    assert list(inspect.signature(eng.run).parameters) == ["inp"]


# --- determinism ------------------------------------------------------------------------------------
def test_row_order_changes_results_only_at_floating_point_tolerance():
    xc, xt = _data(seed=11, n=60)
    rows = rows_of(c=xc, t=xt)
    a = only(run_ref(rows))
    shuffled = rows[:]
    random.Random(3).shuffle(shuffled)
    b = only(run_ref(shuffled))
    for f in ("mean_control", "mean_treatment", "effect", "effect_se", "ci_lower", "ci_upper", "p_value"):
        assert getattr(b, f) == pytest.approx(getattr(a, f), rel=1e-9)


# --- handoff: Component 3 render -> real Postgres -> engine ------------------------------------------
def test_rendered_metric_runs_through_postgres_into_the_engine(user_db):
    from core.connectors import ConnectionSpec, create_connector
    from core.metrics import ParamSpec, ParamType, check_columns, render

    sql = ("SELECT CASE WHEN g % 2 = 0 THEN 'control' ELSE 'treatment' END AS variant, "
           "(g * 1.5 + (g % 7))::float8 AS value FROM generate_series(1, CAST(:n AS int)) AS g")
    q = render(sql, [ParamSpec(name="n", type=ParamType.INTEGER)], {"n": 60})
    spec = ConnectionSpec(type="postgres", secret=user_db["password"],
                          config={"host": user_db["host"], "port": user_db["port"],
                                  "database": user_db["database"], "user": user_db["user"]})
    with create_connector(spec) as c:
        r = c.run_query(q.sql, q.params)
    check_columns(r.columns)
    out = run_engine(create_engine("reference"), REF_PIN, CONT, r.columns, r.rows, "control", {})
    c0 = only(out)
    assert (c0.n_control, c0.n_treatment) == (30, 30) and math.isfinite(c0.p_value)


# --- independence --------------------------------------------------------------------------------------
def test_core_engines_imports_no_db_models_services_or_connectors():
    forbidden = ("core.db", "core.models", "core.services", "core.connectors")
    root = Path(__file__).resolve().parents[2] / "core" / "engines"
    for path in root.glob("*.py"):
        for node in ast.walk(ast.parse(path.read_text())):
            mods = ([a.name for a in node.names] if isinstance(node, ast.Import)
                    else [node.module or ""] if isinstance(node, ast.ImportFrom) else [])
            for m in mods:
                assert not m.startswith(forbidden), f"{path.name} imports {m}"
