"""Invariant tests for the job services (Component 1): submission, state, fencing, results.
Real PostgreSQL; concurrency uses real threads, each with its own session and connection.

Navigation:
  submission and idempotency   same key + request -> same job; different request -> conflict; races
  config hash                  stable, order-independent, sensitive to every field, ignores the key
  pinning                      later registrations never change an existing job
  claiming                     compare-and-set: exactly one winner, also under 12 threads
  leases and reclaim           database clock, expiry, reclaim, retry cap
  fencing and zombies          a superseded worker can change nothing and store nothing
  retry                        fail -> requeue under the cap; primitive only
  completion                   result + succeeded together; atomic with the caller's transaction
  traceability                 result with the full chain
  transaction boundary         services never commit
  independence                 which modules may import what
"""
import ast
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest
from cryptography.fernet import Fernet
from pydantic import SecretStr
from sqlalchemy import func, select, text

from core.db.session import get_engine, get_session_factory, session_scope
from core.engines import registry as engine_registry
from core.metrics import MetricType, ParamSpec, ParamType
from core.models import Job, Result
from core.security.secrets import ENV_VAR
from core.services.connections import create_connection
from core.services.engine_registry import create_engine_entry, register_engine_version
from core.services.errors import IdempotencyConflict, NotFound, StaleClaim
from core.services.job_state import (claim, fail_job, find_expired, find_stale_queued, get_job, heartbeat,
                                     reclaim_expired, requeue)
from core.services.job_submission import compute_config_hash, submit_job
from core.services.metric_registry import create_metric, register_metric_version
from core.services.result_store import complete_job, get_result_with_trace
from tests.fakes import FakeEngine

OUTPUT = {"contract_version": 1, "comparisons": []}


@pytest.fixture(autouse=True)
def secret_key(monkeypatch):
    monkeypatch.setenv(ENV_VAR, Fernet.generate_key().decode())


@pytest.fixture
def factory(db_url):
    return get_session_factory(get_engine(db_url))


@pytest.fixture
def graph(factory):
    engine_registry.register("fake_mean_difference")(FakeEngine)
    try:
        with session_scope(factory) as s:
            conn = create_connection(s, "c", "postgres", {"host": "h", "database": "d", "user": "u"}, SecretStr("pw"))
            metric = create_metric(s, "revenue")
            mv, _ = register_metric_version(s, metric.id, "SELECT :a", [ParamSpec(name="a", type=ParamType.STRING)],
                                            MetricType.CONTINUOUS)
            engine = create_engine_entry(s, "fake")
            ev, _ = register_engine_version(s, engine.id, "fake_mean_difference")
            ids = {"conn": conn.id, "metric": metric.id, "mv": mv.id, "engine": engine.id, "ev": ev.id}
        yield ids
    finally:
        engine_registry.unregister("fake_mean_difference")


_counter = iter(range(10_000))


def request(graph, **over):
    base = dict(metric_version_id=graph["mv"], engine_version_id=graph["ev"], connection_id=graph["conn"],
                parameters={"a": "x"}, engine_params={"confidence_level": 0.95}, control_variant="control",
                idempotency_key=f"key-{next(_counter)}")
    base.update(over)
    return base


def submit(factory, graph, **over):
    with session_scope(factory) as s:
        job, created = submit_job(s, **request(graph, **over))
        return job.id, created


def new_job(factory, graph, **over):
    return submit(factory, graph, **over)[0]


def parallel(n, fn):
    barrier = threading.Barrier(n)

    def run(i):
        barrier.wait()
        try:
            return fn(i)
        except Exception as e:  # noqa: BLE001  collected so a test can count outcomes per exception type
            return e
    with ThreadPoolExecutor(max_workers=n) as pool:
        return list(pool.map(run, range(n)))


def db_now(factory):
    with session_scope(factory) as s:
        return s.scalar(text("SELECT now()"))


# --- submission and idempotency ----------------------------------------------------------
def test_submit_creates_a_queued_pinned_job_with_json_parameters(factory, graph):
    params = {"d": date(2024, 1, 1), "t": datetime(2024, 1, 1, 12, tzinfo=timezone(timedelta(hours=2))), "n": [1, 2]}
    with session_scope(factory) as s:
        job, created = submit_job(s, **request(graph, parameters=params))
    assert created and (job.status, job.attempt_count, job.max_attempts) == ("queued", 0, 3)
    assert job.parameters == {"d": "2024-01-01", "t": "2024-01-01T12:00:00+02:00", "n": [1, 2]}
    assert (job.metric_version_id, job.engine_version_id, job.connection_id) == (graph["mv"], graph["ev"], graph["conn"])


def test_replay_returns_the_same_job_and_a_changed_request_conflicts(factory, graph):
    req = request(graph, idempotency_key="the-key")
    with session_scope(factory) as s:
        first, c1 = submit_job(s, **req)
    with session_scope(factory) as s:
        again, c2 = submit_job(s, **req)
    assert (c1, c2, again.id) == (True, False, first.id)
    for change in ({"parameters": {"a": "y"}}, {"engine_params": {}}, {"control_variant": "other"}):
        with pytest.raises(IdempotencyConflict):
            with session_scope(factory) as s:
                submit_job(s, **{**req, **change})
    with session_scope(factory) as s:
        assert s.scalar(select(func.count()).select_from(Job)) == 1


def test_twelve_concurrent_identical_submits_create_one_job(factory, graph):
    req = request(graph, idempotency_key="race")

    def go(_):
        with session_scope(factory) as s:
            job, created = submit_job(s, **req)
            return job.id, created
    out = parallel(12, go)
    assert not [o for o in out if isinstance(o, Exception)]
    assert len({jid for jid, _ in out}) == 1 and sum(c for _, c in out) == 1


def test_concurrent_submits_with_one_key_and_different_requests_have_exactly_one_winner(factory, graph):
    def go(i):
        with session_scope(factory) as s:
            return submit_job(s, **request(graph, idempotency_key="contested", parameters={"a": str(i)}))[1]
    out = parallel(8, go)
    assert sum(o is True for o in out) == 1
    assert sum(isinstance(o, IdempotencyConflict) for o in out) == 7


def test_distinct_keys_with_the_same_request_are_distinct_jobs(factory, graph):
    ids = {new_job(factory, graph) for _ in range(3)}
    assert len(ids) == 3


def test_missing_references_are_not_found_and_store_nothing(factory, graph):
    for field in ("metric_version_id", "engine_version_id", "connection_id"):
        with pytest.raises(NotFound):
            with session_scope(factory) as s:
                submit_job(s, **request(graph, **{field: uuid.uuid4()}))
    with session_scope(factory) as s:
        assert s.scalar(select(func.count()).select_from(Job)) == 0


# --- config hash -------------------------------------------------------------------------------
def test_config_hash_is_stable_order_independent_and_sensitive_to_every_field(graph):
    a = dict(metric_version_id=graph["mv"], engine_version_id=graph["ev"], connection_id=graph["conn"],
             parameters={"x": 1, "y": 2}, engine_params={"p": 1}, control_variant="c")
    h = compute_config_hash(**a)
    assert h == compute_config_hash(**a) and len(h) == 64
    assert h == compute_config_hash(**{**a, "parameters": {"y": 2, "x": 1}})
    changes = {"metric_version_id": uuid.uuid4(), "engine_version_id": uuid.uuid4(), "connection_id": uuid.uuid4(),
               "parameters": {"x": 1}, "engine_params": {"p": 2}, "control_variant": "t"}
    assert len({compute_config_hash(**{**a, k: v}) for k, v in changes.items()} | {h}) == 7
    assert "idempotency_key" not in compute_config_hash.__code__.co_varnames


def test_the_stored_hash_ignores_the_key_so_equal_requests_share_a_hash(factory, graph):
    a, b = new_job(factory, graph), new_job(factory, graph)
    with session_scope(factory) as s:
        assert get_job(s, a).config_hash == get_job(s, b).config_hash and a != b


# --- pinning ----------------------------------------------------------------------------------------
def test_a_job_keeps_its_pinned_versions_when_newer_ones_are_registered(factory, graph):
    jid = new_job(factory, graph)
    with session_scope(factory) as s:
        v2, created = register_metric_version(s, graph["metric"], "SELECT :a, 2",
                                              [ParamSpec(name="a", type=ParamType.STRING)], MetricType.CONTINUOUS)
        assert created and v2.version_number == 2
    with session_scope(factory) as s:
        assert get_job(s, jid).metric_version_id == graph["mv"]


# --- claiming ------------------------------------------------------------------------------------------
def test_claim_sets_running_attempt_and_a_database_clock_lease(factory, graph):
    jid = new_job(factory, graph)
    before = db_now(factory)
    with session_scope(factory) as s:
        job = claim(s, jid, "w1", 60)
    assert (job.status, job.claimed_by, job.attempt_count, job.started_at is not None) == ("running", "w1", 1, True)
    assert timedelta(seconds=55) < job.lease_expires_at - before < timedelta(seconds=70)


def test_claim_returns_none_unless_the_job_is_queued(factory, graph):
    jid = new_job(factory, graph)
    with session_scope(factory) as s:
        assert claim(s, jid, "w1", 60) is not None
    with session_scope(factory) as s:
        assert claim(s, jid, "w2", 60) is None                          # running
        assert claim(s, uuid.uuid4(), "w2", 60) is None                 # unknown
    with pytest.raises(ValueError):
        with session_scope(factory) as s:
            claim(s, jid, "w", 0)


def test_twelve_concurrent_claims_have_exactly_one_winner(factory, graph):
    jid = new_job(factory, graph)

    def go(i):
        with session_scope(factory) as s:
            job = claim(s, jid, f"w{i}", 60)
            return None if job is None else job.claimed_by
    winners = [o for o in parallel(12, go) if o is not None]
    assert len(winners) == 1 and not any(isinstance(o, Exception) for o in winners)
    with session_scope(factory) as s:
        assert (get_job(s, jid).attempt_count, get_job(s, jid).claimed_by) == (1, winners[0])


# --- leases and reclaim -------------------------------------------------------------------------------------
def test_heartbeat_extends_the_lease_for_the_owner_only(factory, graph):
    jid = new_job(factory, graph)
    with session_scope(factory) as s:
        first = claim(s, jid, "w1", 5).lease_expires_at
    with session_scope(factory) as s:
        assert heartbeat(s, jid, "w1", 1, 600).lease_expires_at > first + timedelta(seconds=300)
    for worker, attempt in (("other", 1), ("w1", 2)):
        with pytest.raises(StaleClaim):
            with session_scope(factory) as s:
                heartbeat(s, jid, worker, attempt, 60)
    with pytest.raises(NotFound):
        with session_scope(factory) as s:
            heartbeat(s, uuid.uuid4(), "w1", 1, 60)


def test_a_live_lease_cannot_be_reclaimed(factory, graph):
    jid = new_job(factory, graph)
    with session_scope(factory) as s:
        claim(s, jid, "w1", 600)
    with session_scope(factory) as s:
        assert reclaim_expired(s, jid, "w2", 60) is None
        assert find_expired(s) == []


def test_an_expired_lease_is_reclaimed_by_a_new_owner_with_a_new_attempt(factory, graph):
    jid = new_job(factory, graph)
    with session_scope(factory) as s:
        claim(s, jid, "w1", 0.2)
    time.sleep(0.5)
    with session_scope(factory) as s:
        assert find_expired(s) == [jid]
        job = reclaim_expired(s, jid, "w2", 60)
    assert (job.status, job.claimed_by, job.attempt_count) == ("running", "w2", 2)
    with session_scope(factory) as s:
        assert find_expired(s) == []


def test_reclaim_at_the_retry_cap_fails_the_job_instead(factory, graph):
    jid = new_job(factory, graph, max_attempts=1)
    with session_scope(factory) as s:
        claim(s, jid, "w1", 0.2)
    time.sleep(0.5)
    with session_scope(factory) as s:
        job = reclaim_expired(s, jid, "w2", 60)
    assert (job.status, job.failure_code, job.attempt_count, job.finished_at is not None) == \
           ("failed", "max_attempts_exceeded", 1, True)
    with session_scope(factory) as s:
        assert requeue(s, jid) is None                                   # no attempts left


def test_concurrent_reclaims_have_exactly_one_winner(factory, graph):
    jid = new_job(factory, graph)
    with session_scope(factory) as s:
        claim(s, jid, "w1", 0.2)
    time.sleep(0.5)

    def go(i):
        with session_scope(factory) as s:
            job = reclaim_expired(s, jid, f"r{i}", 60)
            return None if job is None else job.claimed_by
    winners = [o for o in parallel(8, go) if o is not None]
    assert len(winners) == 1
    with session_scope(factory) as s:
        assert get_job(s, jid).attempt_count == 2


# --- fencing and zombies ---------------------------------------------------------------------------------------
def test_a_superseded_worker_can_change_nothing_and_store_nothing(factory, graph):
    jid = new_job(factory, graph)
    with session_scope(factory) as s:
        claim(s, jid, "zombie", 0.2)                                     # attempt 1
    time.sleep(0.5)
    with session_scope(factory) as s:
        reclaim_expired(s, jid, "owner", 60)                             # attempt 2
    for action in (lambda s: heartbeat(s, jid, "zombie", 1, 60),
                   lambda s: fail_job(s, jid, "zombie", 1, "boom", "late failure"),
                   lambda s: complete_job(s, jid, "zombie", 1, OUTPUT)):
        with pytest.raises(StaleClaim):
            with session_scope(factory) as s:
                action(s)
    with session_scope(factory) as s:
        job = get_job(s, jid)
        assert (job.status, job.claimed_by, job.attempt_count, job.failure_code) == ("running", "owner", 2, None)
        assert s.scalar(select(func.count()).select_from(Result)) == 0
        complete_job(s, jid, "owner", 2, OUTPUT)                         # the real owner still finishes
    with session_scope(factory) as s:
        assert get_job(s, jid).status == "succeeded"


def test_an_expired_but_not_yet_reclaimed_worker_may_still_finish(factory, graph):
    jid = new_job(factory, graph)
    with session_scope(factory) as s:
        claim(s, jid, "slow", 0.2)
    time.sleep(0.5)                                                      # lease lapsed, nobody reclaimed
    with session_scope(factory) as s:
        complete_job(s, jid, "slow", 1, OUTPUT)
    with session_scope(factory) as s:
        assert get_job(s, jid).status == "succeeded"


def test_wrong_worker_or_wrong_attempt_cannot_complete_or_fail(factory, graph):
    jid = new_job(factory, graph)
    with session_scope(factory) as s:
        claim(s, jid, "w1", 60)
    for worker, attempt in (("w2", 1), ("w1", 2), ("w1", 0)):
        for action in (lambda s: complete_job(s, jid, worker, attempt, OUTPUT),
                       lambda s: fail_job(s, jid, worker, attempt, "x", "y")):
            with pytest.raises(StaleClaim):
                with session_scope(factory) as s:
                    action(s)
    with pytest.raises(NotFound):
        with session_scope(factory) as s:
            complete_job(s, uuid.uuid4(), "w1", 1, OUTPUT)


# --- retry --------------------------------------------------------------------------------------------------------------
def test_fail_then_requeue_then_claim_a_second_attempt(factory, graph):
    jid = new_job(factory, graph, max_attempts=2)
    with session_scope(factory) as s:
        claim(s, jid, "w1", 60)
    with session_scope(factory) as s:
        failed = fail_job(s, jid, "w1", 1, "query_timeout", "x" * 900)
    assert (failed.status, failed.failure_code, len(failed.failure_message), failed.claimed_by) == \
           ("failed", "query_timeout", 500, None)
    with session_scope(factory) as s:
        assert requeue(s, jid).status == "queued"
    with session_scope(factory) as s:
        job = claim(s, jid, "w2", 60)
    assert (job.attempt_count, job.claimed_by) == (2, "w2")
    with session_scope(factory) as s:
        fail_job(s, jid, "w2", 2, "query_timeout", "again")
    with session_scope(factory) as s:
        assert requeue(s, jid) is None                                   # cap reached: stays failed
        assert get_job(s, jid).status == "failed"


def test_requeue_only_applies_to_failed_jobs_and_codes_are_validated(factory, graph):
    jid = new_job(factory, graph)
    with session_scope(factory) as s:
        assert requeue(s, jid) is None                                   # queued
        claim(s, jid, "w1", 60)
        assert requeue(s, jid) is None                                   # running
    for bad in ("Bad Code", "", "9x", "x" * 65):
        with pytest.raises(ValueError):
            with session_scope(factory) as s:
                fail_job(s, jid, "w1", 1, bad, "m")


def test_stale_queued_jobs_can_be_listed_for_any_recovery_strategy(factory, graph):
    jid = new_job(factory, graph)
    with session_scope(factory) as s:
        assert find_stale_queued(s, 3600) == []
    time.sleep(0.4)
    with session_scope(factory) as s:
        assert find_stale_queued(s, 0.2) == [jid]


# --- completion --------------------------------------------------------------------------------------------------------------
def test_complete_writes_the_result_and_succeeded_together_and_clears_old_failures(factory, graph):
    jid = new_job(factory, graph)
    with session_scope(factory) as s:
        claim(s, jid, "w1", 60)
        fail_job(s, jid, "w1", 1, "query_timeout", "slow")
        requeue(s, jid)
        claim(s, jid, "w2", 60)
    with session_scope(factory) as s:
        result = complete_job(s, jid, "w2", 2, OUTPUT)
        assert result.job_id == jid
    with session_scope(factory) as s:
        job = get_job(s, jid)
        assert (job.status, job.failure_code, job.failure_message, job.claimed_by) == ("succeeded", None, None, None)
        assert job.finished_at is not None and s.scalar(select(func.count()).select_from(Result)) == 1


def test_completing_twice_is_refused_and_leaves_one_result(factory, graph):
    jid = new_job(factory, graph)
    with session_scope(factory) as s:
        claim(s, jid, "w1", 60)
    with session_scope(factory) as s:
        complete_job(s, jid, "w1", 1, OUTPUT)
    with pytest.raises(StaleClaim):
        with session_scope(factory) as s:
            complete_job(s, jid, "w1", 1, OUTPUT)
    with session_scope(factory) as s:
        assert s.scalar(select(func.count()).select_from(Result)) == 1


def test_two_workers_racing_to_complete_the_same_claim_store_one_result(factory, graph):
    jid = new_job(factory, graph)
    with session_scope(factory) as s:
        claim(s, jid, "w1", 60)

    def go(_):
        with session_scope(factory) as s:
            complete_job(s, jid, "w1", 1, OUTPUT)
            return "ok"
    out = parallel(2, go)
    assert sorted(type(o).__name__ if isinstance(o, Exception) else o for o in out) == ["StaleClaim", "ok"]
    with session_scope(factory) as s:
        assert s.scalar(select(func.count()).select_from(Result)) == 1


def test_complete_is_atomic_with_the_callers_transaction(factory, graph):
    jid = new_job(factory, graph)
    with session_scope(factory) as s:
        claim(s, jid, "w1", 60)
    with pytest.raises(RuntimeError):
        with session_scope(factory) as s:
            complete_job(s, jid, "w1", 1, OUTPUT)
            raise RuntimeError("the caller fails after the service ran")
    with session_scope(factory) as s:
        assert get_job(s, jid).status == "running" and s.scalar(select(func.count()).select_from(Result)) == 0
        complete_job(s, jid, "w1", 1, OUTPUT)                            # and the worker can still finish


def test_complete_requires_a_contract_version(factory, graph):
    jid = new_job(factory, graph)
    with session_scope(factory) as s:
        claim(s, jid, "w1", 60)
    for bad in ({}, {"contract_version": "1"}, {"contract_version": True}):
        with pytest.raises(ValueError):
            with session_scope(factory) as s:
                complete_job(s, jid, "w1", 1, bad)


# --- traceability ----------------------------------------------------------------------------------------------------------------
def test_result_with_trace_has_the_whole_chain_and_no_empty_link(factory, graph):
    jid = new_job(factory, graph, parameters={"a": "exp_42"}, control_variant="control")
    with session_scope(factory) as s:
        assert get_result_with_trace(s, jid) is None                     # no result yet
        claim(s, jid, "w1", 60)
        complete_job(s, jid, "w1", 1, OUTPUT)
    with session_scope(factory) as s:
        t = get_result_with_trace(s, jid)
    assert (t.metric_name, t.metric_version_number, t.engine_name, t.engine_version_number) == ("revenue", 1, "fake", 1)
    assert (t.engine_registry_name, t.engine_declared_version) == ("fake_mean_difference", "0.1")
    assert (t.parameters, t.control_variant, t.connection_id, t.output) == ({"a": "exp_42"}, "control", graph["conn"], OUTPUT)
    assert all(v is not None for v in vars(t).values())
    with pytest.raises(NotFound):
        with session_scope(factory) as s:
            get_result_with_trace(s, uuid.uuid4())


# --- transaction boundary --------------------------------------------------------------------------------------------------------
def test_submit_never_commits_so_a_rolled_back_caller_leaves_no_job(factory, graph):
    with pytest.raises(RuntimeError):
        with session_scope(factory) as s:
            submit_job(s, **request(graph))
            raise RuntimeError("boom")
    with session_scope(factory) as s:
        assert s.scalar(select(func.count()).select_from(Job)) == 0


# --- independence -----------------------------------------------------------------------------------------------------------------
def _imports(path):
    out = []
    for node in ast.walk(ast.parse(path.read_text())):
        out += ([a.name for a in node.names] if isinstance(node, ast.Import)
                else [node.module or ""] if isinstance(node, ast.ImportFrom) else [])
    return out


def test_only_the_registry_and_connection_services_import_the_other_components():
    root = Path(__file__).resolve().parents[2] / "core" / "services"
    for name in ("job_submission.py", "job_state.py", "result_store.py", "versioning.py"):
        assert not [m for m in _imports(root / name) if m.startswith(("core.connectors", "core.engines", "core.metrics"))], name
    assert any(m.startswith("core.connectors") for m in _imports(root / "connections.py"))


def test_a_stored_secret_is_decrypted_in_exactly_one_service_module():
    root = Path(__file__).resolve().parents[2] / "core"
    users = [p.name for p in root.rglob("*.py")
             if p.parent.name != "security" and "decrypt(" in p.read_text()]
    assert users == ["connections.py"]
