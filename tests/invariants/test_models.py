"""Database-rule tests for core/models + migrations 0002/0003, written in RAW SQL on purpose:
the rules must hold no matter which code writes, so these tests bypass the services entirely.
Real PostgreSQL only.

Navigation:
  append-only tables       UPDATE and DELETE rejected on versions, connection, result
  check constraints        bad values rejected by the database
  job insert guard         a job starts queued with attempt_count 0
  pinned columns           nine job columns never change after insert
  legal transitions        every illegal status pair rejected; attempt_count and lease rules
  running iff claimed      claimant and lease are set exactly while running
  result iff succeeded     checked at commit; a second result rejected; result append-only
  references               RESTRICT: nothing a job points at can be deleted
  independence             core/models and core/db never import core/services
"""
import ast
import json
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from core.db.session import get_engine

HASH = "a" * 64


@pytest.fixture
def eng(db_url):
    return get_engine(db_url)


def run(eng, sql, **params):
    with eng.begin() as c:
        return c.execute(text(sql), params)


def fails(eng, sql, match=None, **params):
    with pytest.raises(DBAPIError) as e:
        run(eng, sql, **params)
    if match:
        assert match in str(e.value), str(e.value)


def seed(eng, key="k1", max_attempts=3):
    """A minimal valid graph plus one queued job. Returns the ids."""
    with eng.begin() as c:
        sql = lambda q, **p: c.execute(text(q), p).scalar()  # noqa: E731
        conn = sql("INSERT INTO connection (name, type, config, encrypted_secret) "
                   "VALUES ('c', 'postgres', '{}', 'tok') RETURNING id")
        m = sql("INSERT INTO metric (name) VALUES (:n) RETURNING id", n=f"m-{key}")
        mv = sql("INSERT INTO metric_version (metric_id, version_number, sql_template, param_spec, metric_type, "
                 "content_hash) VALUES (:m, 1, 'SELECT 1', '[]', 'continuous', :h) RETURNING id", m=m, h=HASH)
        e = sql("INSERT INTO engine (name) VALUES (:n) RETURNING id", n=f"e-{key}")
        ev = sql("INSERT INTO engine_version (engine_id, version_number, engine_name, declared_version, "
                 "contract_version) VALUES (:e, 1, 'reference', '1.0.0', 1) RETURNING id", e=e)
        job = sql("INSERT INTO job (max_attempts, metric_version_id, engine_version_id, connection_id, parameters, "
                  "engine_params, control_variant, config_hash, idempotency_key) "
                  "VALUES (:ma, :mv, :ev, :c, '{}', '{}', 'control', :h, :k) RETURNING id",
                  ma=max_attempts, mv=mv, ev=ev, c=conn, h=HASH, k=key)
    return {"conn": conn, "metric": m, "mv": mv, "engine": e, "ev": ev, "job": job}


CLAIM = ("UPDATE job SET status='running', claimed_by='w1', lease_expires_at=now() + interval '1 hour', "
         "attempt_count = attempt_count + 1, started_at = now() WHERE id=:j")
FINISH_OK = ("UPDATE job SET status='succeeded', claimed_by=NULL, lease_expires_at=NULL, finished_at=now() "
             "WHERE id=:j")
FINISH_FAIL = ("UPDATE job SET status='failed', claimed_by=NULL, lease_expires_at=NULL, finished_at=now(), "
               "failure_code='query_timeout', failure_message='timed out' WHERE id=:j")
ADD_RESULT = "INSERT INTO result (job_id, output, contract_version) VALUES (:j, '{}', 1)"


def to_state(eng, ids, state):
    j = ids["job"]
    if state == "queued":
        return
    run(eng, CLAIM, j=j)
    if state == "failed":
        run(eng, FINISH_FAIL, j=j)
    elif state == "succeeded":
        with eng.begin() as c:                    # both writes in one transaction, as the service does
            c.execute(text(FINISH_OK), {"j": j})
            c.execute(text(ADD_RESULT), {"j": j})


# --- append-only tables -----------------------------------------------------------------
@pytest.mark.parametrize("table,set_clause", [
    ("connection", "name='x'"), ("metric", "name='x'"), ("metric_version", "sql_template='x'"),
    ("engine", "name='x'"), ("engine_version", "declared_version='x'"), ("result", "contract_version=2"),
])
def test_append_only_tables_reject_update_and_delete(eng, table, set_clause):
    ids = seed(eng)
    to_state(eng, ids, "succeeded")                  # makes a result row exist
    fails(eng, f"UPDATE {table} SET {set_clause}", "forbidden on immutable table")
    fails(eng, f"DELETE FROM {table}", "forbidden on immutable table")


# --- check constraints -----------------------------------------------------------------------
def test_catalog_check_constraints(eng):
    ids = seed(eng)
    bad = [
        ("INSERT INTO metric_version (metric_id, version_number, sql_template, param_spec, metric_type, content_hash) "
         "VALUES (:m, 0, 's', '[]', 'continuous', :h)", "ck_metric_version_version_number_positive"),
        ("INSERT INTO metric_version (metric_id, version_number, sql_template, param_spec, metric_type, content_hash) "
         "VALUES (:m, 2, 's', '[]', 'bogus', :h)", "ck_metric_version_metric_type_valid"),
        ("INSERT INTO metric_version (metric_id, version_number, sql_template, param_spec, metric_type, content_hash) "
         "VALUES (:m, 2, 's', '[]', 'continuous', 'short')", "ck_metric_version_content_hash_length"),
        ("INSERT INTO metric_version (metric_id, version_number, sql_template, param_spec, metric_type, content_hash) "
         "VALUES (:m, 1, 's', '[]', 'continuous', :h)", "uq_metric_version_metric_id_version_number"),
        ("INSERT INTO engine_version (engine_id, version_number, engine_name, declared_version, contract_version) "
         "VALUES (:e, 1, 'x', '1', 1)", "uq_engine_version_engine_id_version_number"),
        ("INSERT INTO engine_version (engine_id, version_number, engine_name, declared_version, contract_version) "
         "VALUES (:e, 2, '', '1', 1)", "ck_engine_version_name_and_version_not_empty"),
        ("INSERT INTO connection (name, type, config, encrypted_secret) VALUES ('', 'postgres', '{}', 't')",
         "ck_connection_name_and_type_not_empty"),
        ("INSERT INTO metric (name) VALUES ('m-k1')", "uq_metric_name"),
    ]
    for sql, name in bad:
        fails(eng, sql, name, m=ids["metric"], e=ids["engine"], h=HASH)


# --- job insert guard and constraints -------------------------------------------------------------
def test_job_defaults_and_insert_guard(eng):
    ids = seed(eng)
    row = run(eng, "SELECT status, attempt_count, claimed_by FROM job WHERE id=:j", j=ids["job"]).one()
    assert tuple(row) == ("queued", 0, None)
    base = ("INSERT INTO job (status, attempt_count, max_attempts, metric_version_id, engine_version_id, connection_id,"
            " parameters, engine_params, control_variant, config_hash, idempotency_key{extra}) "
            "VALUES (:s, :a, 3, :mv, :ev, :c, '{{}}', '{{}}', 'c', :h, :k{vals})")
    fails(eng, base.format(extra="", vals=""), "inserted as queued", s="running", a=0, mv=ids["mv"], ev=ids["ev"],
          c=ids["conn"], h=HASH, k="x1")
    fails(eng, base.format(extra="", vals=""), "inserted as queued", s="queued", a=1, mv=ids["mv"], ev=ids["ev"],
          c=ids["conn"], h=HASH, k="x2")


@pytest.mark.parametrize("override,name", [
    ("idempotency_key=''", "ck_job_idempotency_key_length"), ("max_attempts=0", "ck_job_max_attempts_positive"),
    ("config_hash='short'", "ck_job_config_hash_length"), ("control_variant=''", "ck_job_control_variant_not_empty"),
])
def test_job_check_constraints_on_insert(eng, override, name):
    ids = seed(eng)
    cols = {"max_attempts": "3", "idempotency_key": "'fresh'", "config_hash": f"'{HASH}'", "control_variant": "'c'"}
    k, v = override.split("=", 1)
    cols[k] = v
    sql = ("INSERT INTO job (max_attempts, metric_version_id, engine_version_id, connection_id, parameters, "
           f"engine_params, control_variant, config_hash, idempotency_key) VALUES ({cols['max_attempts']}, :mv, :ev, :c,"
           f" '{{}}', '{{}}', {cols['control_variant']}, {cols['config_hash']}, {cols['idempotency_key']})")
    fails(eng, sql, name, mv=ids["mv"], ev=ids["ev"], c=ids["conn"])


def test_idempotency_key_is_unique_and_references_must_exist(eng):
    ids = seed(eng, key="dup")
    sql = ("INSERT INTO job (max_attempts, metric_version_id, engine_version_id, connection_id, parameters, "
           "engine_params, control_variant, config_hash, idempotency_key) "
           "VALUES (3, :mv, :ev, :c, '{}', '{}', 'c', :h, :k)")
    fails(eng, sql, "uq_job_idempotency_key", mv=ids["mv"], ev=ids["ev"], c=ids["conn"], h=HASH, k="dup")
    fails(eng, sql, "fk_job_metric_version_id", mv="00000000-0000-0000-0000-000000000000", ev=ids["ev"],
          c=ids["conn"], h=HASH, k="other")


# --- pinned columns -----------------------------------------------------------------------------------
def test_every_pinned_job_column_is_immutable(eng):
    ids = seed(eng)
    other = seed(eng, key="k2")                              # a second valid graph to point at
    changes = {
        "metric_version_id": ("metric_version_id=:v", {"v": other["mv"]}),
        "engine_version_id": ("engine_version_id=:v", {"v": other["ev"]}),
        "connection_id": ("connection_id=:v", {"v": other["conn"]}),
        "parameters": ("parameters='{\"x\": 1}'", {}),
        "engine_params": ("engine_params='{\"x\": 1}'", {}),
        "control_variant": ("control_variant='other'", {}),
        "config_hash": (f"config_hash='{'b' * 64}'", {}),
        "idempotency_key": ("idempotency_key='changed'", {}),
        "max_attempts": ("max_attempts=9", {}),
    }
    for col, (clause, params) in changes.items():
        fails(eng, f"UPDATE job SET {clause} WHERE id=:j", f"column {col} is immutable", j=ids["job"], **params)


def test_an_unchanged_pinned_value_may_be_rewritten(eng):
    ids = seed(eng)
    run(eng, CLAIM, j=ids["job"])
    run(eng, "UPDATE job SET parameters = parameters, lease_expires_at = now() + interval '2 hours' WHERE id=:j",
        j=ids["job"])      # only a *change* is refused; a running job's update is a legal heartbeat


# --- legal transitions ---------------------------------------------------------------------------------------
SET = {
    "queued": "status='queued', claimed_by=NULL, lease_expires_at=NULL, finished_at=NULL",
    "running": "status='running', claimed_by='w2', lease_expires_at=now() + interval '1 hour', finished_at=NULL, "
               "attempt_count=attempt_count + 1",
    "succeeded": "status='succeeded', claimed_by=NULL, lease_expires_at=NULL, finished_at=now(), "
                 "failure_code=NULL, failure_message=NULL",
    "failed": "status='failed', claimed_by=NULL, lease_expires_at=NULL, finished_at=now(), "
              "failure_code='x', failure_message='y'",
}
LEGAL = {("queued", "running"), ("running", "running"), ("running", "succeeded"), ("running", "failed"),
         ("failed", "queued")}


ILLEGAL = [(a, b) for a in SET for b in SET if (a, b) not in LEGAL]   # 11 of the 16 pairs; legal ones: service tests


@pytest.mark.parametrize("src,dst", ILLEGAL)
def test_every_illegal_status_transition_is_rejected(eng, src, dst):
    ids = seed(eng)
    to_state(eng, ids, src)
    fails(eng, f"UPDATE job SET {SET[dst]} WHERE id=:j", "illegal job transition", j=ids["job"])


def test_succeeded_is_terminal_even_for_harmless_looking_updates(eng):
    ids = seed(eng)
    to_state(eng, ids, "succeeded")
    fails(eng, "UPDATE job SET claimed_by=NULL WHERE id=:j", "illegal job transition", j=ids["job"])


def test_attempt_count_rules(eng):
    ids = seed(eng)
    j = ids["job"]
    fails(eng, "UPDATE job SET status='running', claimed_by='w', lease_expires_at=now()+interval '1 hour' "
               "WHERE id=:j", "must start a new attempt", j=j)               # claim without a new attempt
    fails(eng, "UPDATE job SET status='running', claimed_by='w', lease_expires_at=now()+interval '1 hour', "
               "attempt_count=2 WHERE id=:j", "exactly one", j=j)             # skipping attempts
    run(eng, CLAIM, j=j)                                                       # attempt 1
    fails(eng, "UPDATE job SET attempt_count=0 WHERE id=:j", "exactly one", j=j)            # decrease
    fails(eng, "UPDATE job SET claimed_by='thief' WHERE id=:j", "requires a new attempt", j=j)  # steal, same attempt
    fails(eng, "UPDATE job SET claimed_by='thief', attempt_count=2 WHERE id=:j",
          "lease is still valid", j=j)                                         # steal a live lease
    fails(eng, "UPDATE job SET status='failed', claimed_by=NULL, lease_expires_at=NULL, finished_at=now(), "
               "failure_code='x', failure_message='y', attempt_count=2 WHERE id=:j", "cannot change attempt_count", j=j)


def test_reclaim_is_allowed_only_after_the_lease_expired_and_heartbeats_are_free(eng):
    ids = seed(eng)
    j = ids["job"]
    run(eng, CLAIM, j=j)
    run(eng, "UPDATE job SET lease_expires_at = now() + interval '2 hours' WHERE id=:j", j=j)    # heartbeat
    run(eng, "UPDATE job SET lease_expires_at = now() - interval '1 minute' WHERE id=:j", j=j)   # lease lapsed
    run(eng, "UPDATE job SET claimed_by='w2', attempt_count=2, lease_expires_at=now() + interval '1 hour' "
             "WHERE id=:j", j=j)                                                                 # reclaim
    assert tuple(run(eng, "SELECT claimed_by, attempt_count FROM job WHERE id=:j", j=j).one()) == ("w2", 2)


def test_requeue_is_refused_at_the_retry_cap(eng):
    ids = seed(eng, max_attempts=1)
    to_state(eng, ids, "failed")                                                   # attempt 1 of 1
    fails(eng, f"UPDATE job SET {SET['queued']} WHERE id=:j", "retry cap reached", j=ids["job"])
    ids2 = seed(eng, key="k2", max_attempts=2)
    to_state(eng, ids2, "failed")                                                  # attempt 1 of 2
    run(eng, f"UPDATE job SET {SET['queued']} WHERE id=:j", j=ids2["job"])         # allowed


# --- running iff claimed -------------------------------------------------------------------------------------------------
def test_running_iff_claimant_and_lease_and_finished_iff_terminal(eng):
    ids = seed(eng)
    j = ids["job"]
    fails(eng, "UPDATE job SET status='running', attempt_count=1 WHERE id=:j", "ck_job_running_iff_claimed", j=j)
    run(eng, CLAIM, j=j)
    fails(eng, "UPDATE job SET lease_expires_at=NULL WHERE id=:j", "ck_job_running_iff_claimed", j=j)
    fails(eng, "UPDATE job SET status='failed', claimed_by=NULL, lease_expires_at=NULL, finished_at=now() "
               "WHERE id=:j", "ck_job_failed_has_code", j=j)
    fails(eng, "UPDATE job SET status='failed', claimed_by=NULL, lease_expires_at=NULL, failure_code='x', "
               "failure_message='y' WHERE id=:j", "ck_job_finished_iff_terminal", j=j)
    fails(eng, "UPDATE job SET status='failed', claimed_by=NULL, lease_expires_at=NULL, finished_at=now(), "
               "failure_code='Bad Code!', failure_message='y' WHERE id=:j", "ck_job_failure_code_format", j=j)
    fails(eng, "UPDATE job SET status='failed', claimed_by=NULL, lease_expires_at=NULL, finished_at=now(), "
               "failure_code='x', failure_message=repeat('m', 501) WHERE id=:j", "ck_job_failure_message_length", j=j)


# --- result iff succeeded ------------------------------------------------------------------------------------------------------
def test_succeeded_without_a_result_cannot_commit(eng):
    ids = seed(eng)
    run(eng, CLAIM, j=ids["job"])
    with pytest.raises(DBAPIError, match="succeeded but has no result"):
        run(eng, FINISH_OK, j=ids["job"])                                     # commit fails (deferred trigger)
    assert run(eng, "SELECT status FROM job WHERE id=:j", j=ids["job"]).scalar() == "running"


def test_a_result_for_a_job_that_did_not_succeed_cannot_commit(eng):
    ids = seed(eng)
    run(eng, CLAIM, j=ids["job"])
    with pytest.raises(DBAPIError, match="requires the job to be succeeded"):
        run(eng, ADD_RESULT, j=ids["job"])
    assert run(eng, "SELECT count(*) FROM result").scalar() == 0


def test_result_and_succeeded_commit_together_in_either_order(eng):
    a, b = seed(eng, key="a"), seed(eng, key="b")
    for ids, first_result in ((a, False), (b, True)):
        run(eng, CLAIM, j=ids["job"])
        with eng.begin() as c:
            if first_result:                                # result first, then the status flip
                c.execute(text(ADD_RESULT), {"j": ids["job"]})
                c.execute(text(FINISH_OK), {"j": ids["job"]})
            else:
                c.execute(text(FINISH_OK), {"j": ids["job"]})
                c.execute(text(ADD_RESULT), {"j": ids["job"]})
    assert run(eng, "SELECT count(*) FROM result").scalar() == 2


def test_a_second_result_for_one_job_is_rejected(eng):
    ids = seed(eng)
    to_state(eng, ids, "succeeded")
    fails(eng, ADD_RESULT, "uq_result_job_id", j=ids["job"])


# --- references ------------------------------------------------------------------------------------------------------------------------
def test_nothing_a_job_references_can_be_deleted(eng):
    ids = seed(eng)
    for table, col in (("metric_version", "mv"), ("engine_version", "ev"), ("connection", "conn")):
        fails(eng, f"DELETE FROM {table} WHERE id=:i", i=ids[col])
    to_state(eng, ids, "succeeded")
    fails(eng, "DELETE FROM job WHERE id=:j", j=ids["job"])                      # its result references it


def test_job_updated_at_is_database_maintained(eng):
    ids = seed(eng)
    before = run(eng, "SELECT updated_at FROM job WHERE id=:j", j=ids["job"]).scalar()
    run(eng, CLAIM, j=ids["job"])
    assert run(eng, "SELECT updated_at FROM job WHERE id=:j", j=ids["job"]).scalar() >= before


# --- independence -------------------------------------------------------------------------------------------------------------------------
def test_models_and_db_never_import_services():
    root = Path(__file__).resolve().parents[2] / "core"
    for pkg in ("models", "db"):
        for path in (root / pkg).rglob("*.py"):
            for node in ast.walk(ast.parse(path.read_text())):
                mods = ([a.name for a in node.names] if isinstance(node, ast.Import)
                        else [node.module or ""] if isinstance(node, ast.ImportFrom) else [])
                assert not any(m.startswith("core.services") for m in mods), f"{path.name} imports services"
