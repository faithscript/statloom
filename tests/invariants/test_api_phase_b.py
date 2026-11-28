"""Invariant tests for the database-backed API (Component 5, Phase B): connections, registration,
jobs. Real PostgreSQL; the publisher is the recording fake (the real Kafka publisher is deferred).

Navigation (one group per invariant):
  startup key validation   the app refuses to start without a valid STATLOOM_SECRET_KEY
  secret never leaves      no response, error, log, row, or OpenAPI response schema carries a secret
  response shapes          responses expose only their declared fields
  registration             connections, metrics, engines: 201 new / 200 identical, typed failures
  submit: validate early   bad input creates no job
  store before publish     the job is committed and visible when its id is published; failure is survivable
  idempotent submit        replay, conflict, 12 concurrent submits
  pinning, normalization   later registrations don't change a job; stored parameters are canonical
  status and result        queued / running / succeeded / failed
  read-only GETs           no INSERT, UPDATE, or DELETE
  database down            503 without details
  independence             (see test_api.py for the AST scan)
"""
import logging
import socket
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor

import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
from sqlalchemy import event, text

from core.api import ApiDeps, create_app
from core.db.session import get_engine, get_session_factory, session_scope
from core.engines import registry as engine_registry
from core.metrics import MetricType
from core.security.secrets import ENV_VAR, SecretsConfigError
from core.services.job_state import claim, fail_job
from core.services.result_store import complete_job
from tests.fakes import FakeEngine, RecordingPublisher

SECRET = "distinctive-api-secret-9921"
OUTPUT = {"contract_version": 1, "comparisons": []}
HOST = {"host": "203.0.113.1", "port": 1, "database": "d", "user": "u"}   # never connected to
SQL = "SELECT variant, value FROM t WHERE d = :d"
DATE_SPEC = [{"name": "d", "type": "date"}]


@pytest.fixture(autouse=True)
def secret_key(monkeypatch):
    monkeypatch.setenv(ENV_VAR, Fernet.generate_key().decode())


@pytest.fixture
def env(db_url):
    publisher = RecordingPublisher()
    factory = get_session_factory(get_engine(db_url))
    app = create_app(ApiDeps(session_factory=factory, publisher=publisher))
    client = TestClient(app, raise_server_exceptions=False)
    return type("Env", (), {"app": app, "client": client, "publisher": publisher, "factory": factory,
                            "engine": get_engine(db_url), "db_url": db_url})


def err(r):
    body = r.json()
    assert set(body) == {"error"} and body["error"]["request_id"] == r.headers["x-request-id"]
    return body["error"]


def register(env, *, metric_type="continuous", parameters=None, engine="reference", suffix="", sql=SQL):
    """Register a connection, metric (version 1), and engine (version 1) through the API."""
    c = env.client
    def ok(r):
        assert r.status_code in (200, 201), r.text           # a setup failure must fail loudly, not as a KeyError
        return r.json()
    conn = ok(c.post("/v1/connections", json={"name": f"c{suffix}", "type": "postgres", "config": HOST,
                                              "secret": "pw"}))
    metric = ok(c.post("/v1/metrics", json={"name": f"m{suffix}"}))
    mv = ok(c.post(f"/v1/metrics/{metric['id']}/versions",
                   json={"sql_template": sql, "metric_type": metric_type,
                         "parameters": DATE_SPEC if parameters is None else parameters}))
    eng = ok(c.post("/v1/engines", json={"name": f"e{suffix}"}))
    ev = ok(c.post(f"/v1/engines/{eng['id']}/versions", json={"engine_name": engine}))
    return {"conn": conn["id"], "metric": metric["id"], "mv": mv["id"], "engine": eng["id"], "ev": ev["id"]}


_keys = iter(range(100_000))


def job_body(ids, **over):
    body = {"metric_version_id": ids["mv"], "engine_version_id": ids["ev"], "connection_id": ids["conn"],
            "parameters": {"d": "2024-01-01"}, "engine_params": {}, "control_variant": "control"}
    body.update(over)
    return body


def submit(env, ids, key=None, **over):
    return env.client.post("/v1/jobs", json=job_body(ids, **over),
                           headers={"Idempotency-Key": key or f"k-{next(_keys)}"})


def job_count(env):
    with env.engine.connect() as c:
        return c.execute(text("SELECT count(*) FROM job")).scalar()


def parallel(n, fn):
    barrier = threading.Barrier(n)

    def run(i):
        barrier.wait()
        return fn(i)
    with ThreadPoolExecutor(max_workers=n) as pool:
        return list(pool.map(run, range(n)))


# --- startup key validation ------------------------------------------------------------------
def test_app_with_a_database_refuses_to_start_without_a_valid_key(env, monkeypatch):
    monkeypatch.delenv(ENV_VAR)
    with pytest.raises(SecretsConfigError, match=ENV_VAR):
        create_app(ApiDeps(session_factory=env.factory))
    bad = "definitely-not-a-fernet-key-but-secret-looking"
    monkeypatch.setenv(ENV_VAR, bad)
    with pytest.raises(SecretsConfigError) as e:
        create_app(ApiDeps(session_factory=env.factory))
    assert bad not in str(e.value)
    assert create_app().state.deps.session_factory is None          # no database: no key needed


# --- secret never leaves ---------------------------------------------------------------------------
def test_the_secret_appears_in_no_response_log_row_or_error(env, caplog):
    with caplog.at_level(logging.DEBUG):
        r = env.client.post("/v1/connections", json={"name": "prod", "type": "postgres", "config": HOST,
                                                     "secret": SECRET})
        assert r.status_code == 201
        cid = r.json()["id"]
        texts = [r.text, env.client.get(f"/v1/connections/{cid}").text]
        # error paths: bad config, unknown top-level field carrying the secret, wrong type for the secret
        bad = [
            env.client.post("/v1/connections", json={"name": "x", "type": "postgres", "secret": SECRET,
                                                     "config": {"host": "h", "password": SECRET}}),
            env.client.post("/v1/connections", json={"name": "x", "type": "nope", "config": {}, "secret": SECRET}),
            env.client.post("/v1/connections", json={"name": "x", "type": "postgres", "config": HOST,
                                                     "secret": SECRET, "leak": SECRET}),
            env.client.post("/v1/connections", json={"name": "x", "type": "postgres", "config": HOST,
                                                     "secret": {"v": SECRET}}),
        ]
        assert [b.status_code for b in bad] == [422, 422, 422, 422]
        texts += [b.text for b in bad]
        # the 500 path: an unexpected failure while encrypting
        import core.services.connections as conns
        original = conns.encrypt
        conns.encrypt = lambda s: (_ for _ in ()).throw(RuntimeError(SECRET))
        try:
            boom = env.client.post("/v1/connections", json={"name": "y", "type": "postgres", "config": HOST,
                                                            "secret": SECRET})
        finally:
            conns.encrypt = original
        assert boom.status_code == 500 and err(boom)["code"] == "internal_error"
        texts.append(boom.text)
    assert all(SECRET not in t for t in texts)
    assert SECRET not in caplog.text
    with env.engine.connect() as c:
        assert SECRET not in c.execute(text("SELECT string_agg(row_to_json(connection)::text, '') FROM connection")).scalar()


def test_openapi_responses_are_secret_free_and_the_request_secret_is_write_only(env):
    spec = env.client.get("/openapi.json").json()
    schemas = spec["components"]["schemas"]

    def refs(node):
        if isinstance(node, dict):
            for k, v in node.items():
                if k == "$ref":
                    yield v.rsplit("/", 1)[-1]
                else:
                    yield from refs(v)
        elif isinstance(node, list):
            for v in node:
                yield from refs(v)
    response_schemas, todo = set(), []
    for ops in spec["paths"].values():
        for op in ops.values():
            for code, resp in op["responses"].items():
                todo += list(refs(resp))
    while todo:
        name = todo.pop()
        if name not in response_schemas:
            response_schemas.add(name)
            todo += list(refs(schemas[name]))
    forbidden = ("secret", "password", "credential", "encrypted", "claimed_by", "lease", "idempotency_key")
    for name in response_schemas:
        assert not any(w in str(schemas[name].get("properties", {}).keys()).lower() for w in forbidden), name
    assert schemas["ConnectionCreate"]["properties"]["secret"].get("writeOnly") is True


# --- response shapes --------------------------------------------------------------------------------------
def test_responses_expose_only_declared_fields(env):
    ids = register(env)
    job = submit(env, ids).json()
    assert set(job) == {"id", "status", "attempt_count", "max_attempts", "metric_version_id", "engine_version_id",
                        "connection_id", "parameters", "engine_params", "control_variant", "config_hash", "failure",
                        "created_at", "started_at", "finished_at"}
    conn = env.client.get(f"/v1/connections/{ids['conn']}").json()
    assert set(conn) == {"id", "name", "type", "config", "created_at"}
    with session_scope(env.factory) as s:
        claim(s, uuid.UUID(job["id"]), "w", 60)
    with session_scope(env.factory) as s:
        complete_job(s, uuid.UUID(job["id"]), "w", 1, OUTPUT)
    result = env.client.get(f"/v1/jobs/{job['id']}/result").json()
    assert set(result) == {"job_id", "contract_version", "result", "trace"}
    assert not {"claimed_by", "lease_expires_at", "encrypted_secret", "idempotency_key"} & set(str(result))


# --- registration -------------------------------------------------------------------------------------------------
def test_connection_registration_and_reads(env):
    r = env.client.post("/v1/connections", json={"name": "prod", "type": "postgres", "config": HOST, "secret": "pw"})
    assert r.status_code == 201 and r.json()["config"] == HOST
    assert env.client.get(f"/v1/connections/{r.json()['id']}").json()["name"] == "prod"
    assert err(env.client.get("/v1/connections/00000000-0000-0000-0000-000000000000"))["code"] == "not_found"
    assert err(env.client.get("/v1/connections/not-a-uuid"))["code"] == "request_invalid"
    bad = env.client.post("/v1/connections", json={"name": "x", "type": "postgres", "config": {"host": "h"},
                                                   "secret": "pw"})
    assert bad.status_code == 422 and err(bad)["code"] == "connection_config_invalid"
    unknown = env.client.post("/v1/connections", json={"name": "x", "type": "mysql", "config": {}, "secret": "pw"})
    assert err(unknown)["code"] == "connection_config_invalid"
    with env.engine.connect() as c:
        assert c.execute(text("SELECT count(*) FROM connection")).scalar() == 1      # failures stored nothing


def test_creating_a_connection_never_connects(env):
    r = env.client.post("/v1/connections", json={"name": "offline", "type": "postgres", "config": HOST,
                                                 "secret": "pw"})
    assert r.status_code == 201                                                       # unreachable host, returns


def test_metric_registration_flow(env):
    m = env.client.post("/v1/metrics", json={"name": "revenue"})
    assert m.status_code == 201
    mid = m.json()["id"]
    dup = env.client.post("/v1/metrics", json={"name": "revenue"})
    assert dup.status_code == 409 and err(dup)["code"] == "already_exists"
    body = {"sql_template": SQL, "metric_type": "continuous", "parameters": DATE_SPEC}
    v1 = env.client.post(f"/v1/metrics/{mid}/versions", json=body)
    again = env.client.post(f"/v1/metrics/{mid}/versions", json=body)
    v2 = env.client.post(f"/v1/metrics/{mid}/versions", json={**body, "sql_template": SQL + " AND x = 1"})
    assert (v1.status_code, again.status_code, v2.status_code) == (201, 200, 201)
    assert again.json()["id"] == v1.json()["id"] and (v1.json()["version_number"], v2.json()["version_number"]) == (1, 2)
    assert len(v1.json()["content_hash"]) == 64
    got = env.client.get(f"/v1/metrics/{mid}/versions/2")
    assert got.status_code == 200 and got.json()["id"] == v2.json()["id"]
    assert err(env.client.get(f"/v1/metrics/{mid}/versions/9"))["code"] == "not_found"


def test_invalid_metric_versions_store_nothing(env):
    mid = env.client.post("/v1/metrics", json={"name": "m"}).json()["id"]
    for body, code in (({"sql_template": "SELECT {{ d }}", "metric_type": "continuous", "parameters": DATE_SPEC},
                        "template_invalid"),
                       ({"sql_template": SQL, "metric_type": "continuous", "parameters": DATE_SPEC + DATE_SPEC},
                        "parameter_invalid")):
        r = env.client.post(f"/v1/metrics/{mid}/versions", json=body)
        assert r.status_code == 422 and err(r)["code"] == code
    unknown = env.client.post("/v1/metrics/00000000-0000-0000-0000-000000000000/versions",
                              json={"sql_template": SQL, "metric_type": "continuous", "parameters": DATE_SPEC})
    assert unknown.status_code == 404
    with env.engine.connect() as c:
        assert c.execute(text("SELECT count(*) FROM metric_version")).scalar() == 0


def test_concurrent_metric_creation_gives_one_201_and_the_rest_409(env):
    out = parallel(8, lambda _: TestClient(env.app).post("/v1/metrics", json={"name": "contested"}).status_code)
    assert sorted(out) == [201] + [409] * 7


def test_engine_registration_flow(env):
    eng = env.client.post("/v1/engines", json={"name": "ref"})
    eid = eng.json()["id"]
    assert eng.status_code == 201 and env.client.post("/v1/engines", json={"name": "ref"}).status_code == 409
    v1 = env.client.post(f"/v1/engines/{eid}/versions", json={"engine_name": "reference"})
    again = env.client.post(f"/v1/engines/{eid}/versions", json={"engine_name": "reference"})
    assert (v1.status_code, again.status_code) == (201, 200) and again.json()["id"] == v1.json()["id"]
    assert (v1.json()["declared_version"], v1.json()["contract_version"]) == ("1.0.0", 1)
    assert env.client.get(f"/v1/engines/{eid}/versions/1").json()["engine_name"] == "reference"
    unknown = env.client.post(f"/v1/engines/{eid}/versions", json={"engine_name": "nope"})
    assert unknown.status_code == 422 and err(unknown)["code"] == "engine_invalid"
    assert env.client.post("/v1/engines/00000000-0000-0000-0000-000000000000/versions",
                           json={"engine_name": "reference"}).status_code == 404


# --- submit: validate early -------------------------------------------------------------------------------------------
def test_bad_submissions_create_no_job(env):
    engine_registry.register("only_continuous")(type("OnlyCont", (FakeEngine,), {
        "name": "only_continuous", "supports": frozenset({MetricType.CONTINUOUS})}))
    try:
        ids = register(env)
        prop = register(env, metric_type="proportion", engine="only_continuous", suffix="2")
        zero = "00000000-0000-0000-0000-000000000000"
        cases = [
            (dict(parameters={"d": "not-a-date"}), 422, "parameter_invalid"),
            (dict(parameters={}), 422, "parameter_invalid"),                               # missing required
            (dict(parameters={"d": "2024-01-01", "extra": 1}), 422, "parameter_invalid"),  # unknown parameter
            (dict(engine_params={"confidence_level": 5}), 422, "engine_params_invalid"),
            (dict(engine_params={"nope": 1}), 422, "engine_params_invalid"),
            (dict(control_variant=""), 422, "request_invalid"),
            (dict(unknown_field=1), 422, "request_invalid"),
            (dict(metric_version_id=zero), 404, "not_found"),
            (dict(engine_version_id=zero), 404, "not_found"),
            (dict(connection_id=zero), 404, "not_found"),
        ]
        for over, status, code in cases:
            r = submit(env, ids, **over)
            assert (r.status_code, err(r)["code"]) == (status, code), over
        mismatch = submit(env, prop, parameters={"d": "2024-01-01"})
        assert mismatch.status_code == 422 and err(mismatch)["code"] == "engine_metric_mismatch"
        assert job_count(env) == 0 and env.publisher.published == []
    finally:
        engine_registry.unregister("only_continuous")


def test_header_and_value_errors_do_not_echo_values(env):
    ids = register(env)
    r = env.client.post("/v1/jobs", json=job_body(ids, parameters={"d": "distinctive-bad-date"}),
                        headers={"Idempotency-Key": "k"})
    assert r.status_code == 422 and "distinctive-bad-date" not in r.text
    r = env.client.post("/v1/jobs", json=job_body(ids, engine_params={"confidence_level": "distinctive-bad"}),
                        headers={"Idempotency-Key": "k"})
    assert r.status_code == 422 and "distinctive-bad" not in r.text
    for headers in ({}, {"Idempotency-Key": ""}, {"Idempotency-Key": "x" * 256}, {"Idempotency-Key": "has space"}):
        r = env.client.post("/v1/jobs", json=job_body(ids), headers=headers)
        assert r.status_code == 422 and err(r)["code"] == "request_invalid"
    assert job_count(env) == 0


# --- store before publish ------------------------------------------------------------------------------------------------
def test_the_job_is_committed_and_visible_when_its_id_is_published(env):
    ids = register(env)
    seen = []

    def on_publish(job_id):
        with env.engine.connect() as other:          # a different connection: sees only committed rows
            seen.append(other.execute(text("SELECT status FROM job WHERE id = :i"), {"i": job_id}).scalar())
    env.publisher.on_publish = on_publish
    r = submit(env, ids)
    assert r.status_code == 202 and seen == ["queued"] and env.publisher.published == [r.json()["id"]]


def test_a_failed_publish_leaves_the_job_queued_and_the_client_still_gets_202(env, caplog):
    ids = register(env)
    for failing in (RecordingPublisher(fail=True), type("Boom", (), {"publish": lambda self, j: 1 / 0})()):
        env.app.state.deps = ApiDeps(session_factory=env.factory, publisher=failing)
        with caplog.at_level(logging.ERROR):
            r = submit(env, ids)
        assert r.status_code == 202 and r.json()["status"] == "queued"
        assert env.client.get(f"/v1/jobs/{r.json()['id']}").json()["status"] == "queued"
        assert "publish failed" in caplog.text and r.json()["id"] not in caplog.text    # no payload logged
        caplog.clear()


def test_without_a_publisher_the_job_is_stored_and_stays_queued(env):
    ids = register(env)
    env.app.state.deps = ApiDeps(session_factory=env.factory, publisher=None)
    r = submit(env, ids)
    assert r.status_code == 202 and r.json()["status"] == "queued" and job_count(env) == 1


# --- idempotent submit ------------------------------------------------------------------------------------------------------------
def test_replay_returns_the_same_job_without_publishing_again(env):
    ids = register(env)
    first = submit(env, ids, key="the-key")
    again = submit(env, ids, key="the-key")
    assert (first.status_code, again.status_code) == (202, 202)
    assert first.json()["id"] == again.json()["id"] and first.headers["location"] == f"/v1/jobs/{first.json()['id']}"
    assert "idempotent-replayed" not in first.headers and again.headers["idempotent-replayed"] == "true"
    assert env.publisher.published == [first.json()["id"]] and job_count(env) == 1


def test_the_same_key_with_a_different_request_is_a_409(env):
    ids = register(env)
    submit(env, ids, key="k")
    for over in (dict(parameters={"d": "2025-02-02"}), dict(control_variant="other"),
                 dict(engine_params={"confidence_level": 0.9})):
        r = submit(env, ids, key="k", **over)
        assert r.status_code == 409 and err(r)["code"] == "idempotency_conflict"
    assert job_count(env) == 1 and len(env.publisher.published) == 1


def test_twelve_concurrent_identical_submits_create_one_job_and_one_publish(env):
    ids = register(env)
    body = job_body(ids)
    out = parallel(12, lambda _: TestClient(env.app).post("/v1/jobs", json=body,
                                                         headers={"Idempotency-Key": "race"}))
    assert {r.status_code for r in out} == {202} and len({r.json()["id"] for r in out}) == 1
    assert job_count(env) == 1 and len(env.publisher.published) == 1
    assert sum(r.headers.get("idempotent-replayed") == "true" for r in out) == 11


# --- pinning and normalization ----------------------------------------------------------------------------------------------------------
def test_a_job_keeps_its_pinned_version_when_a_newer_one_is_registered(env):
    ids = register(env)
    job = submit(env, ids).json()
    env.client.post(f"/v1/metrics/{ids['metric']}/versions",
                    json={"sql_template": SQL + " AND x = 1", "metric_type": "continuous", "parameters": DATE_SPEC})
    assert env.client.get(f"/v1/jobs/{job['id']}").json()["metric_version_id"] == ids["mv"]


def test_stored_parameters_are_canonical(env):
    spec = DATE_SPEC + [{"name": "t", "type": "datetime"}]
    ids = register(env, parameters=spec, sql="SELECT 1 WHERE d = :d AND t = :t")
    a = submit(env, ids, parameters={"d": "2024-01-01", "t": "2024-01-01T12:00:00+02:00"}, engine_params={}).json()
    b = submit(env, ids, parameters={"t": "2024-01-01T10:00:00+00:00", "d": "2024-01-01"},
               engine_params={"confidence_level": 0.95}).json()
    assert a["parameters"] == {"d": "2024-01-01", "t": "2024-01-01T10:00:00Z"}
    assert a["engine_params"] == {"confidence_level": 0.95}              # defaults filled in
    assert a["config_hash"] == b["config_hash"] and a["id"] != b["id"]   # equal requests, distinct keys


# --- status and result ----------------------------------------------------------------------------------------------------------------------
def test_status_and_result_for_every_state(env):
    ids = register(env)
    assert err(env.client.get("/v1/jobs/00000000-0000-0000-0000-000000000000"))["code"] == "not_found"
    assert err(env.client.get("/v1/jobs/00000000-0000-0000-0000-000000000000/result"))["code"] == "not_found"
    jid = submit(env, ids).json()["id"]
    r = env.client.get(f"/v1/jobs/{jid}/result")
    assert r.status_code == 409 and err(r)["code"] == "result_not_ready" and "queued" in err(r)["message"]
    with session_scope(env.factory) as s:
        claim(s, uuid.UUID(jid), "w1", 60)
    r = env.client.get(f"/v1/jobs/{jid}/result")
    assert r.status_code == 409 and "running" in err(r)["message"]
    job = env.client.get(f"/v1/jobs/{jid}").json()
    assert (job["status"], job["attempt_count"], job["started_at"] is not None) == ("running", 1, True)
    with session_scope(env.factory) as s:
        complete_job(s, uuid.UUID(jid), "w1", 1, OUTPUT)
    done = env.client.get(f"/v1/jobs/{jid}/result")
    assert done.status_code == 200
    t = done.json()["trace"]
    assert done.json()["result"] == OUTPUT and (t["metric_name"], t["metric_version_number"]) == ("m", 1)
    assert (t["engine_registry_name"], t["connection_id"], t["control_variant"]) == ("reference", ids["conn"], "control")
    assert env.client.get(f"/v1/jobs/{jid}").json()["finished_at"] is not None


def test_a_failed_job_reports_a_value_free_failure(env):
    ids = register(env)
    jid = submit(env, ids).json()["id"]
    with session_scope(env.factory) as s:
        claim(s, uuid.UUID(jid), "w1", 60)
        fail_job(s, uuid.UUID(jid), "w1", 1, "query_timeout", "the query exceeded its time limit")
    job = env.client.get(f"/v1/jobs/{jid}").json()
    assert job["status"] == "failed" and job["failure"] == {"code": "query_timeout",
                                                            "message": "the query exceeded its time limit"}
    r = env.client.get(f"/v1/jobs/{jid}/result")
    assert r.status_code == 409 and err(r)["code"] == "job_failed" and "query_timeout" in err(r)["message"]


# --- read-only GETs ------------------------------------------------------------------------------------------------------------------------------
def test_get_endpoints_make_no_writes(env):
    ids = register(env)
    jid = submit(env, ids).json()["id"]
    statements = []

    def record(conn, cursor, statement, *a):
        statements.append(statement.lstrip().split(None, 1)[0].upper())
    event.listen(env.engine, "before_cursor_execute", record)
    try:
        for path in (f"/v1/jobs/{jid}", f"/v1/jobs/{jid}/result", f"/v1/connections/{ids['conn']}",
                     f"/v1/metrics/{ids['metric']}/versions/1", f"/v1/engines/{ids['engine']}/versions/1",
                     "/readyz", "/healthz"):
            env.client.get(path)
    finally:
        event.remove(env.engine, "before_cursor_execute", record)
    assert statements and not {"INSERT", "UPDATE", "DELETE"} & set(statements)


# --- database down ----------------------------------------------------------------------------------------------------------------------------------
def test_an_unreachable_database_is_a_503_without_details(caplog):
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    url = f"postgresql+psycopg://postgres@127.0.0.1:{port}/postgres"
    c = TestClient(create_app(ApiDeps(session_factory=get_session_factory(get_engine(url)))),
                   raise_server_exceptions=False)
    with caplog.at_level(logging.DEBUG):
        r = c.post("/v1/connections", json={"name": "x", "type": "postgres", "config": HOST, "secret": "pw"})
    assert r.status_code == 503 and err(r)["code"] == "service_unavailable"
    assert str(port) not in r.text and str(port) not in caplog.text
