"""Invariant tests for core/api (Component 5), Phase A: no registration or job routes yet.

Navigation (one group per invariant):
  one error shape          every failure source -> status, code, request id, no value echo
  unexpected errors        500 without detail; only the exception type is logged
  request bounds           unknown fields, bad types, size limit, missing length, bad JSON
  request ids              generated, echoed, injection-safe, in logs and error bodies
  access log               method / path / status only: no bodies, queries, or headers
  stateless validate       hash, rendering, hostile values, typed errors, no database
  health and readiness     /healthz always; /readyz honest (real Postgres)
  openapi contract         generates, error shape shared, no secret-like fields
  publisher                protocol + typed failure + recording fake
  independence             the API never connects, queries, or runs engines
Phase B groups (secret never leaves, store-before-publish, idempotent submit, pinning,
status/result, read-only GETs) are written with Component 1's services.
"""
import ast
import logging
import socket
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from core.api import ApiDeps, ApiError, PublishFailed, create_app
from core.connectors import ConfigInvalid, UnknownConnectorType
from core.db.session import get_engine, get_session_factory
from core.engines import EngineDeclarationInvalid, UnknownEngine
from core.metrics import (MetricError, MetricType, ParameterInvalid, ParamSpec, ParamType,
                          TemplateInvalid, content_hash)
from tests.fakes import RecordingPublisher

VALIDATE = "/v1/metrics/validate"
SPEC_A = {"name": "a", "type": "string"}
GOOD = {"sql_template": "SELECT :a", "metric_type": "continuous", "parameters": [SPEC_A]}


@pytest.fixture
def client():
    return TestClient(create_app(), raise_server_exceptions=False)


def err(resp):
    body = resp.json()
    assert set(body) == {"error"} and set(body["error"]) == {"code", "message", "request_id"}
    assert body["error"]["request_id"] == resp.headers["x-request-id"]
    return body["error"]


# --- one error shape --------------------------------------------------------
@pytest.mark.parametrize("exc,status,code", [
    (TemplateInvalid("t"), 422, "template_invalid"),
    (ParameterInvalid("p"), 422, "parameter_invalid"),
    (MetricError("m"), 422, "metric_invalid"),
    (UnknownEngine("u"), 422, "engine_invalid"),
    (EngineDeclarationInvalid("d"), 422, "engine_invalid"),
    (ConfigInvalid("c"), 422, "connection_config_invalid"),
    (UnknownConnectorType("k"), 422, "connection_config_invalid"),
    (ApiError(409, "idempotency_conflict", "same key, different request"), 409, "idempotency_conflict"),
])
def test_every_typed_failure_maps_to_one_status_code_and_body_shape(client, exc, status, code):
    def boom():
        raise exc
    client.app.add_api_route("/_boom", boom)
    r = client.get("/_boom")
    assert r.status_code == status and err(r)["code"] == code


def test_framework_failures_share_the_shape(client):
    assert err(client.get("/nope"))["code"] == "not_found"
    assert err(client.get(VALIDATE))["code"] == "method_not_allowed"
    assert err(client.post(VALIDATE, json={}))["code"] == "request_invalid"


# --- unexpected errors ---------------------------------------------------------
def test_unexpected_exception_is_a_500_without_detail_and_logs_only_the_type(client, caplog):
    def boom():
        raise ValueError("distinctive-secret-text")
    client.app.add_api_route("/_boom", boom)
    with caplog.at_level(logging.DEBUG):
        r = client.get("/_boom")
    e = err(r)
    assert r.status_code == 500 and e["code"] == "internal_error"
    assert "distinctive-secret-text" not in r.text
    assert "distinctive-secret-text" not in caplog.text and "ValueError" in caplog.text


# --- request bounds ----------------------------------------------------------------
def test_unknown_fields_and_wrong_types_rejected_without_echoing_values(client):
    r = client.post(VALIDATE, json={**GOOD, "surprise": "distinctive-value-1"})
    assert r.status_code == 422 and "surprise" in err(r)["message"]
    assert "distinctive-value-1" not in r.text
    r = client.post(VALIDATE, json={**GOOD, "metric_type": "distinctive-value-2"})
    assert r.status_code == 422 and "distinctive-value-2" not in r.text
    r = client.post(VALIDATE, json={**GOOD, "parameters": [{**SPEC_A, "bogus": "distinctive-value-3"}]})
    assert r.status_code == 422 and "distinctive-value-3" not in r.text


def test_bad_json_and_bounds(client):
    assert client.post(VALIDATE, content=b"{not json", headers={"content-type": "application/json"}).status_code == 422
    too_many = [{"name": f"p{i}", "type": "string"} for i in range(51)]
    assert client.post(VALIDATE, json={**GOOD, "parameters": too_many}).status_code == 422
    assert client.post(VALIDATE, json={**GOOD, "sql_template": "x" * 70_000}).status_code == 422
    assert client.post(VALIDATE, json={**GOOD, "sql_template": ""}).status_code == 422


def test_oversized_body_rejected_before_any_work():
    small = TestClient(create_app(ApiDeps(max_body_bytes=200)), raise_server_exceptions=False)
    r = small.post(VALIDATE, json={**GOOD, "sql_template": "SELECT :a -- " + "x" * 500})
    assert r.status_code == 413 and err(r)["code"] == "request_too_large"
    assert small.post(VALIDATE, json=GOOD).status_code == 200  # a small body still works


def test_body_without_content_length_is_refused(client):
    r = client.post(VALIDATE, content=(chunk for chunk in [b'{"a":', b"1}"]),
                    headers={"content-type": "application/json"})
    assert r.status_code == 411 and err(r)["code"] == "length_required"


# --- request ids -----------------------------------------------------------------------
def test_request_ids_generated_echoed_and_injection_safe(client):
    a, b = client.get("/healthz"), client.get("/healthz")
    assert a.headers["x-request-id"] != b.headers["x-request-id"]
    assert client.get("/healthz", headers={"X-Request-ID": "trace-123.abc"}).headers["x-request-id"] == "trace-123.abc"
    for bad in ("has space", "x" * 65, "semi;colon", "a/b"):
        got = client.get("/healthz", headers={"X-Request-ID": bad}).headers["x-request-id"]
        assert got != bad and len(got) == 32
    r = client.get("/nope", headers={"X-Request-ID": "trace-9"})
    assert err(r)["request_id"] == "trace-9"


# --- access log ---------------------------------------------------------------------------
def test_access_log_has_no_bodies_queries_or_headers(client, caplog):
    with caplog.at_level(logging.INFO, logger="statloom.api.access"):
        client.post(VALIDATE + "?token=query-secret", json={**GOOD, "sample_values": {"a": "body-secret"}},
                    headers={"Authorization": "Bearer header-secret", "X-Request-ID": "rid-1"})
    assert "POST /v1/metrics/validate 200" in caplog.text and "rid=rid-1" in caplog.text
    for leaked in ("query-secret", "body-secret", "header-secret", "Bearer"):
        assert leaked not in caplog.text


# --- stateless validate ----------------------------------------------------------------------
def test_validate_returns_the_content_hash(client):
    r = client.post(VALIDATE, json=GOOD)
    assert r.status_code == 200 and r.json()["valid"] is True and r.json()["rendered"] is None
    assert r.json()["content_hash"] == content_hash(
        "SELECT :a", [ParamSpec(name="a", type=ParamType.STRING)], MetricType.CONTINUOUS)
    assert client.post(VALIDATE, json=GOOD).json() == r.json()           # deterministic


@pytest.mark.parametrize("hostile", ["'; DROP TABLE t; --", '" OR 1=1', "%s", ":other", "{{ 7*7 }}"])
def test_hostile_sample_values_never_reach_the_sql_text(client, hostile):
    benign = client.post(VALIDATE, json={**GOOD, "sample_values": {"a": "web"}}).json()["rendered"]
    bad = client.post(VALIDATE, json={**GOOD, "sample_values": {"a": hostile}}).json()["rendered"]
    assert bad["sql"] == benign["sql"] and bad["params"] == {"a": hostile} and hostile not in bad["sql"]


def test_optional_block_preview(client):
    body = {"sql_template": "SELECT 1 {% if seg is not none %}WHERE s = :seg{% endif %}",
            "metric_type": "proportion", "parameters": [{"name": "seg", "type": "string", "required": False}]}
    assert client.post(VALIDATE, json={**body, "sample_values": {}}).json()["rendered"]["params"] == {}
    with_seg = client.post(VALIDATE, json={**body, "sample_values": {"seg": "x"}}).json()["rendered"]
    assert with_seg["params"] == {"seg": "x"} and ":seg" in with_seg["sql"]


@pytest.mark.parametrize("sql,specs,code", [
    ("SELECT {{ a }}", [SPEC_A], "template_invalid"),                    # value output
    ("SELECT :a, :b", [SPEC_A], "template_invalid"),                     # undeclared
    ("SELECT 1", [SPEC_A], "template_invalid"),                          # declared, unused
    ("SELECT :a {% for i in a %}{% endfor %}", [SPEC_A], "template_invalid"),
    ("SELECT :a", [SPEC_A, SPEC_A], "parameter_invalid"),                # duplicate name
    ("SELECT :a", [{"name": "1bad", "type": "string"}], "parameter_invalid"),
])
def test_invalid_templates_are_typed_422s(client, sql, specs, code):
    r = client.post(VALIDATE, json={**GOOD, "sql_template": sql, "parameters": specs})
    assert r.status_code == 422 and err(r)["code"] == code


def test_bad_sample_values_are_parameter_errors_that_do_not_echo_the_value(client):
    for values in ({"a": 123456789}, {"nope": "distinctive-value-4"}, {}):
        r = client.post(VALIDATE, json={**GOOD, "sample_values": values})
        assert r.status_code == 422 and err(r)["code"] == "parameter_invalid"
        assert "123456789" not in r.text and "distinctive-value-4" not in r.text


def test_validate_needs_no_database_or_publisher():
    app = create_app()                                    # no session factory, no publisher
    assert app.state.deps.session_factory is None and app.state.deps.publisher is None
    assert TestClient(app).post(VALIDATE, json=GOOD).status_code == 200


# --- health and readiness -----------------------------------------------------------------------
def test_liveness_is_always_up_and_readiness_without_a_database_is_503(client):
    assert client.get("/healthz").json() == {"status": "ok"}
    r = client.get("/readyz")
    assert r.status_code == 503 and err(r)["code"] == "not_configured"


def test_ready_on_a_migrated_database(db_url):
    c = TestClient(create_app(ApiDeps(session_factory=get_session_factory(get_engine(db_url)))))
    assert c.get("/readyz").json() == {"status": "ready"}


def test_not_ready_when_the_schema_is_behind_and_liveness_stays_up(fresh_db_url):
    c = TestClient(create_app(ApiDeps(session_factory=get_session_factory(get_engine(fresh_db_url)))),
                   raise_server_exceptions=False)
    r = c.get("/readyz")
    assert r.status_code == 503 and err(r)["code"] == "schema_mismatch"
    assert "alembic" not in r.text.lower() and fresh_db_url not in r.text
    assert c.get("/healthz").status_code == 200


def test_not_ready_when_postgres_is_unreachable():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]                         # a port nothing listens on
    url = f"postgresql+psycopg://postgres@127.0.0.1:{port}/postgres"
    c = TestClient(create_app(ApiDeps(session_factory=get_session_factory(get_engine(url)))),
                   raise_server_exceptions=False)
    r = c.get("/readyz")
    assert r.status_code == 503 and err(r)["code"] == "service_unavailable"
    assert str(port) not in r.text and c.get("/healthz").status_code == 200


# --- openapi contract -------------------------------------------------------------------------------
def test_openapi_generates_with_one_error_schema_and_no_secret_like_fields(client):
    spec = client.get("/openapi.json").json()
    assert "ErrorOut" in spec["components"]["schemas"]
    for path, ops in spec["paths"].items():
        for method, op in ops.items():
            ok = [c for c in op["responses"] if c.startswith("2")]
            assert ok and "content" in op["responses"][ok[0]], f"{method} {path} has no response model"
            for code in ("422", "500"):
                ref = op["responses"].get(code, {}).get("content", {}).get("application/json", {}).get("schema", {})
                assert ref.get("$ref", "").endswith("ErrorOut"), f"{method} {path} {code} is not ErrorOut"
    text = str(spec["components"]["schemas"]).lower()
    for word in ("secret", "password", "credential"):
        assert word not in text


# --- publisher ----------------------------------------------------------------------------------------
def test_recording_publisher_and_typed_failure():
    p = RecordingPublisher()
    p.publish("job-1")
    assert p.published == ["job-1"]
    bad = RecordingPublisher(fail=True)
    with pytest.raises(PublishFailed) as e:
        bad.publish("job-2")
    assert str(e.value) == "" and bad.published == []     # nothing recorded as sent, no payload in the error


def test_publish_callback_sees_the_moment_of_publish():
    seen = []
    RecordingPublisher(on_publish=seen.append).publish("job-3")
    assert seen == ["job-3"]


# --- independence ---------------------------------------------------------------------------------------
def test_the_api_never_connects_queries_or_runs_engines():
    root = Path(__file__).resolve().parents[2] / "core" / "api"
    forbidden = ("core.connectors.postgres", "core.engines.runner")
    for path in root.rglob("*.py"):
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            mods = ([a.name for a in node.names] if isinstance(node, ast.Import)
                    else [node.module or ""] if isinstance(node, ast.ImportFrom) else [])
            for m in mods:
                assert not m.startswith(forbidden), f"{path.name} imports {m}"
            if isinstance(node, ast.Attribute):
                assert node.attr not in ("run_query", "run_engine"), f"{path.name} uses {node.attr}"
