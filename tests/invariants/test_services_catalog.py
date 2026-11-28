"""Invariant tests for the catalog services (Component 1): versioning, metric and engine registries.
Real PostgreSQL only (row locks, triggers, and unique constraints are Postgres behavior).

Navigation:
  sequential versioning     next number, identical content is a no-op, new content is a new version
  concurrency               12 threads: gapless numbers; identical content creates one version
  validation before storage invalid templates / unknown engines store nothing
  load                      stored versions come back as plain values a worker can use
  transaction boundary      services never commit; a rolled-back caller leaves nothing
  engine registry           declared before registered; version bump; pin accepted by the runner
"""
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest
from sqlalchemy import func, select

from core.db.session import get_engine, get_session_factory, session_scope
from core.engines import (CONTRACT_VERSION, EngineDeclarationInvalid, UnknownEngine, run_engine)
from core.engines import registry as engine_registry
from core.metrics import MetricType, ParamSpec, ParamType, TemplateInvalid, render
from core.models import EngineVersion, Metric, MetricVersion
from core.services.engine_registry import (create_engine_entry, load_pinned, register_engine_version)
from core.services.errors import AlreadyExists, NotFound
from core.services.metric_registry import create_metric, load_renderable, register_metric_version
from core.services.versioning import next_version_number
from tests.fakes import FakeEngine

CONT = MetricType.CONTINUOUS
SPEC = [ParamSpec(name="a", type=ParamType.STRING)]


@pytest.fixture
def factory(db_url):
    return get_session_factory(get_engine(db_url))


def new_metric(factory, name="m"):
    with session_scope(factory) as s:
        return create_metric(s, name).id


# --- sequential versioning --------------------------------------------------
def test_versions_are_numbered_from_one_and_identical_content_is_a_no_op(factory):
    mid = new_metric(factory)
    with session_scope(factory) as s:
        v1, created1 = register_metric_version(s, mid, "SELECT :a", SPEC, CONT)
        v1_again, created_again = register_metric_version(s, mid, "SELECT :a", SPEC, CONT)
        v2, created2 = register_metric_version(s, mid, "SELECT :a, 1", SPEC, CONT)
    assert (v1.version_number, created1) == (1, True)
    assert (v1_again.id, created_again) == (v1.id, False)
    assert (v2.version_number, created2) == (2, True)
    with session_scope(factory) as s:
        # a change back to the first content is a NEW version (only the *latest* is compared)
        v3, created3 = register_metric_version(s, mid, "SELECT :a", SPEC, CONT)
        assert (v3.version_number, created3) == (3, True)
        assert next_version_number(s, Metric, mid) == 4


def test_a_changed_spec_or_type_is_a_new_version(factory):
    mid = new_metric(factory)
    with session_scope(factory) as s:
        register_metric_version(s, mid, "SELECT :a", SPEC, CONT)
        _, c1 = register_metric_version(s, mid, "SELECT :a", SPEC, MetricType.PROPORTION)
        _, c2 = register_metric_version(s, mid, "SELECT :a", [ParamSpec(name="a", type=ParamType.STRING, required=False)],
                                        MetricType.PROPORTION)
    assert c1 and c2


def test_duplicate_names_rejected_and_unknown_parent_is_not_found(factory):
    new_metric(factory, "dup")
    with pytest.raises(AlreadyExists):
        with session_scope(factory) as s:
            create_metric(s, "dup")
    import uuid
    with pytest.raises(NotFound):
        with session_scope(factory) as s:
            register_metric_version(s, uuid.uuid4(), "SELECT :a", SPEC, CONT)


# --- concurrency ------------------------------------------------------------------
def _parallel(n, fn):
    barrier = threading.Barrier(n)

    def run(i):
        barrier.wait()
        return fn(i)
    with ThreadPoolExecutor(max_workers=n) as pool:
        return list(pool.map(run, range(n)))


def test_concurrent_registrations_produce_gapless_unique_numbers(factory):
    mid = new_metric(factory)

    def register(i):
        with session_scope(factory) as s:
            v, created = register_metric_version(s, mid, f"SELECT :a, {i}", SPEC, CONT)
            return v.version_number, created
    results = _parallel(12, register)
    assert sorted(n for n, _ in results) == list(range(1, 13))
    assert all(created for _, created in results)
    with session_scope(factory) as s:
        assert s.scalar(select(func.count()).select_from(MetricVersion)) == 12


def test_concurrent_identical_registrations_create_exactly_one_version(factory):
    mid = new_metric(factory)

    def register(_):
        with session_scope(factory) as s:
            v, created = register_metric_version(s, mid, "SELECT :a", SPEC, CONT)
            return v.id, created
    results = _parallel(12, register)
    assert len({vid for vid, _ in results}) == 1 and sum(c for _, c in results) == 1


def test_versioning_is_per_entity(factory):
    a, b = new_metric(factory, "a"), new_metric(factory, "b")
    with session_scope(factory) as s:
        assert register_metric_version(s, a, "SELECT :a", SPEC, CONT)[0].version_number == 1
        assert register_metric_version(s, b, "SELECT :a", SPEC, CONT)[0].version_number == 1


# --- validation before storage -------------------------------------------------------
def test_invalid_template_stores_nothing(factory):
    mid = new_metric(factory)
    with pytest.raises(TemplateInvalid):
        with session_scope(factory) as s:
            register_metric_version(s, mid, "SELECT {{ a }}", SPEC, CONT)
    with session_scope(factory) as s:
        assert s.scalar(select(func.count()).select_from(MetricVersion)) == 0


# --- load -------------------------------------------------------------------------------------
def test_load_renderable_returns_plain_values_that_render(factory):
    mid = new_metric(factory)
    sql = "SELECT 1 {% if a is not none %}WHERE x = :a{% endif %}"
    specs = [ParamSpec(name="a", type=ParamType.STRING, required=False)]
    with session_scope(factory) as s:
        v, _ = register_metric_version(s, mid, sql, specs, MetricType.PROPORTION)
        vid = v.id
    with session_scope(factory) as s:
        r = load_renderable(s, vid)
    assert (r.sql_template, r.specs, r.metric_type) == (sql, tuple(specs), MetricType.PROPORTION)
    assert render(r.sql_template, r.specs, {"a": "x"}).params == {"a": "x"}
    with pytest.raises(NotFound):
        import uuid
        with session_scope(factory) as s:
            load_renderable(s, uuid.uuid4())


def test_objects_stay_readable_after_the_session_closes(factory):
    with session_scope(factory) as s:
        m = create_metric(s, "readable")
    assert m.id is not None and m.created_at is not None and m.name == "readable"   # no DetachedInstanceError


# --- transaction boundary ------------------------------------------------------------------------
def test_services_never_commit_so_a_rolled_back_caller_leaves_nothing(factory):
    with pytest.raises(RuntimeError):
        with session_scope(factory) as s:
            m = create_metric(s, "ghost")
            register_metric_version(s, m.id, "SELECT :a", SPEC, CONT)
            raise RuntimeError("caller fails after the services ran")
    with session_scope(factory) as s:
        assert s.scalar(select(func.count()).select_from(Metric)) == 0
        assert s.scalar(select(func.count()).select_from(MetricVersion)) == 0


# --- engine registry -------------------------------------------------------------------------------
@pytest.fixture
def fake_engine_registered():
    engine_registry.register("fake_mean_difference")(FakeEngine)
    yield
    engine_registry.unregister("fake_mean_difference")


def test_engine_versions_and_no_op_on_identical_declaration(factory, fake_engine_registered):
    with session_scope(factory) as s:
        eid = create_engine_entry(s, "fake").id
    with session_scope(factory) as s:
        v1, c1 = register_engine_version(s, eid, "fake_mean_difference")
        v1b, c1b = register_engine_version(s, eid, "fake_mean_difference")
    assert (v1.version_number, c1, v1.contract_version) == (1, True, CONTRACT_VERSION)
    assert (v1b.id, c1b) == (v1.id, False)
    assert (v1.engine_name, v1.declared_version) == ("fake_mean_difference", "0.1")


def test_a_bumped_declared_version_is_a_new_engine_version(factory, fake_engine_registered, monkeypatch):
    with session_scope(factory) as s:
        eid = create_engine_entry(s, "fake").id
        register_engine_version(s, eid, "fake_mean_difference")
    monkeypatch.setattr(FakeEngine, "version", "0.2")
    with session_scope(factory) as s:
        v, created = register_engine_version(s, eid, "fake_mean_difference")
    assert (v.version_number, created, v.declared_version) == (2, True, "0.2")


def test_unknown_or_incomplete_engines_are_not_stored(factory):
    with session_scope(factory) as s:
        eid = create_engine_entry(s, "e").id
    with pytest.raises(UnknownEngine):
        with session_scope(factory) as s:
            register_engine_version(s, eid, "nope")
    with session_scope(factory) as s:
        assert s.scalar(select(func.count()).select_from(EngineVersion)) == 0


def test_concurrent_engine_registrations_stay_gapless(factory):
    """8 differently-named engines registered in parallel under one engine entry."""
    names = [f"fake_par_{i}" for i in range(8)]
    for n in names:
        engine_registry.register(n)(type(f"E_{n}", (FakeEngine,), {"name": n}))
    try:
        with session_scope(factory) as s:
            eid = create_engine_entry(s, "par").id

        def register(i):
            with session_scope(factory) as s:
                return register_engine_version(s, eid, names[i])[0].version_number
        assert sorted(_parallel(8, register)) == list(range(1, 9))
    finally:
        for n in names:
            engine_registry.unregister(n)


def test_load_pinned_gives_an_engine_the_runner_accepts(factory, fake_engine_registered):
    with session_scope(factory) as s:
        eid = create_engine_entry(s, "fake").id
        vid = register_engine_version(s, eid, "fake_mean_difference")[0].id
    with session_scope(factory) as s:
        engine, pin = load_pinned(s, vid)
    rows = [("c", 1.0), ("c", 2.0), ("t", 2.0), ("t", 3.0)]
    out = run_engine(engine, pin, CONT, ("variant", "value"), rows, "c", {})
    assert out.comparisons[0].treatment == "t"
    import uuid
    with pytest.raises(NotFound):
        with session_scope(factory) as s:
            load_pinned(s, uuid.uuid4())
