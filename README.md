# Statloom

Statloom is a distributed platform for running statistical analyses. Bring your own database, SQL queries, and Python engine, and Statloom handles data retrieval, job execution, and storing results.

> Under active development. See [Development Status](#development-status).

## Motivation

Teams often have their own databases, metric definitions, and statistical methods but still need infrastructure to execute analyses concurrently, distribute workloads, handle failures and retries, scale as demand grows, and store results.

Statloom separates this infrastructure from the analysis itself, allowing teams to focus on what they measure and how they measure it without having to build and maintain their own distributed execution system.

## Core Capabilities

*Planned for V1 unless marked otherwise.*

- **Database extensibility:** a connector interface with PostgreSQL as the first provider and read-only access. Credentials are stored only as encrypted tokens (built).
- **Metrics as code:** parameterized SQL templates written against the user's own schema. *Built:* template and parameter validation, rendering to bound parameters, and a content hash. Versions are stored immutably and numbered without gaps (built).
- **Pluggable statistical engines:** custom Python engines behind a standard input/output contract, plus a reference engine for continuous and proportional metrics. *Built:* the contract, input and output validation, an engine registry, and the reference engine. Engine versions are stored immutably, with the same numbering (built).
- **Persistence and job lifecycle:** jobs pin exact versions and parameters, are submitted idempotently, and move through a database-enforced state machine with leases and fencing so a stale worker cannot overwrite the current owner. *Built:* the storage, rules, and services. *Planned:* the workers that use them.
- **Distributed execution:** asynchronous jobs through Kafka, concurrent workers on Kubernetes, with retries and duplicate-safe processing.
- **Job submission API:** a REST API to register connections, metrics, and engines, submit jobs, and fetch results, publishing job ids to Kafka. *Built:* the API foundation (one error format, request limits and ids, health and readiness checks) and a stateless endpoint that validates and previews a metric template. *Planned:* registration, job submit, status and result endpoints, and the Kafka publisher.
- **Traceable analysis:** every result traces back to the exact metric version, engine version, connection, and parameters that produced it.

## System Architecture

Green is built, blue is partly built, yellow is planned, and the dashed edge marks an open design decision.

```mermaid
flowchart TD
Client([Client])
API["REST API<br/>(partly built)"]
K[["Kafka<br/>(planned)"]]
W["Worker x N<br/>(planned)"]
ENG["Statistical engine<br/>(built)"]
MET["Metric templates<br/>(built)"]
CONN["Connector layer<br/>(built)"]
UDB[("User database<br/>read-only")]
DB["Persistence layer<br/>(built)"]
PG[("Statloom Postgres<br/>registries, jobs, results<br/>(built)")]

Client -->|"Submit / Poll / Fetch"| API
API -->|"1. Store job"| DB
API -.->|"2. Publish job"| K
K <-->|"3. Deliver job<br/>8. Commit offset"| W
W -->|"4. Claim job"| DB
W -->|"5a. Render metric"| MET
W -->|"5b. Execute SQL"| CONN
CONN --> UDB
W -->|"6. Compute statistics"| ENG
W -->|"7. Save result and status"| DB
DB --> PG

classDef built fill:#d4edda,stroke:#2e7d32,color:#000
classDef partial fill:#d6eaf8,stroke:#2874a6,color:#000
classDef planned fill:#fff3cd,stroke:#b58900,color:#000
class CONN,DB,MET,ENG,PG built
class API partial
class K,W planned
```

Workers claim jobs, render the metric template into a query, fetch metric data through a connector, run the selected engine, and write the result and job status in a single transaction.

## Design Principles

- **Modularity:** database-specific logic, metric definitions, and statistical engines are swappable without changing core orchestration.
- **Correctness:** behavior is defined by explicit, testable invariants.
- **Database-enforced invariants:** constraints, triggers, and foreign keys come first. Application services come second and handle races.
- **Fault tolerance:** retries are safe, duplicates cannot produce duplicate results, and crashed workers do not strand jobs.
- **Separation of concerns:** the API, workers, connectors, and engines interact only through defined interfaces.

## Technology Stack

| Area | Choice | Responsibility |
|---|---|---|
| Language | Python | Core services and statistical engines |
| API | FastAPI, Pydantic, Uvicorn | REST interface, request and engine contracts (Uvicorn serves the app) |
| Persistence | PostgreSQL, SQLAlchemy 2.x, Alembic | Versioned registries, jobs, results, migrations; invariants enforced by constraints and triggers |
| Metric templates | Jinja2 (sandboxed) | Optional `{% if %}` blocks in SQL templates; values are never rendered into the SQL |
| Messaging | Apache Kafka (KRaft, single node locally) | Asynchronous job distribution |
| Statistics | NumPy, SciPy | Reference engine (Welch t-test, two-proportion z-test) |
| Secrets | `cryptography` (Fernet) | Encrypted connection credentials, with key rotation |
| Packaging | Docker | One image per service |
| Orchestration | Kubernetes (local cluster) | Worker replicas and recovery |

SQLAlchemy, psycopg, Alembic, Pydantic, Jinja2, NumPy, SciPy, `cryptography`, FastAPI, Uvicorn, HTTPX (the API test client), and pytest are in use today. The rest is planned.

## Development Status

### V1 components

| # | Component | Status |
|---|---|---|
| 1 | Core data model | ✅ Built: tables, migrations, services, credential encryption, and the job lifecycle with database-enforced rules |
| 2 | Database connector layer | ✅ Built: interface, registry, and PostgreSQL provider |
| 3 | Metric registry | ✅ Built: template validation, rendering, the shared SQL scanner, and versioned storage |
| 4 | Statistical engine interface and reference engine | ✅ Built: contract, validation, registry, reference engine, and versioned storage |
| 5 | REST API | ✅ Built at the API layer: registration, connections, job submit/status/result, and stateless metric validation, tested against real PostgreSQL with a fake publisher. Not yet wired: the real Kafka publisher and a database-backed Uvicorn configuration |
| 6 | Worker service | ⬜ Planned |
| 7 | Infrastructure (Docker, Kubernetes, logging) | ⬜ Planned |
| 8 | Reference implementation and demo | ⬜ Planned |

### Implemented so far

`core/db` provides the transaction boundary, row locking, timestamp and append-only triggers, and a startup schema check. It is covered by 14 invariant tests that run against a real PostgreSQL instance.

`core/connectors` provides a provider-neutral connector interface, a registry, and a read-only PostgreSQL connector with query timeouts, a row cap, bound parameters, and typed, retry-classified errors. It is covered by 39 invariant tests, including a contract suite that runs the same checks against a fake provider and the Postgres one. Its `:name` placeholder scanner lives in `core/connectors/sqlscan.py` and is shared with the metric code.

`core/metrics` provides typed, bounded metric parameters, a sandboxed template validator that allows only `{% if %}` blocks around literal SQL (no value output, calls, loops, or includes), a renderer that returns SQL plus a separate bind dict, a content hash for each definition, and an output-column contract. It is covered by 65 invariant tests, one of which runs a rendered query through the real PostgreSQL connector.

`core/engines` defines one input and output contract for every statistical engine, an engine registry, and a runner that validates the data going in and the result coming out (a buggy custom engine fails one job instead of storing a wrong number). The reference engine compares each treatment with the control using Welch's t-test for continuous metrics and a two-proportion z-test for proportions, reporting the effect, standard error, confidence interval, and p-value. It is covered by 72 invariant tests: results are checked against SciPy and hand calculations, plus seeded simulations of error rates. One test runs a rendered metric through the real connector into the engine.

`core/models`, `core/services`, and `core/security` (migrations 0002 and 0003) are the persistence layer on PostgreSQL. Connections store credentials only as Fernet-encrypted tokens, and several keys can be configured so keys can be rotated. Metrics and engines have immutable versions numbered without gaps, even under concurrent registration; registering identical content returns the existing version. A job pins its metric version, engine version, connection, parameters, and a configuration hash, and is submitted with a client-supplied idempotency key through one atomic insert-or-find, so a retried request returns the original job and a reused key with a different request is rejected. The job lifecycle (claim, lease, heartbeat, reclaim, fail, requeue, complete) is made of compare-and-set operations fenced on a per-claim attempt counter: a worker whose lease expired and whose job was reclaimed can neither update the job nor store a result. The database itself enforces immutability, legal status transitions, and that a result exists exactly when its job has succeeded (checked at commit), so the rules hold even if code bypasses the services. It is covered by 103 invariant tests against real PostgreSQL, using real threads for the concurrency cases and raw SQL to prove the database rules, and by deliberate breakage of the code to confirm the tests notice. These are mechanisms, not a running system: there is no worker yet, so nothing claims or runs jobs, and the REST API stores a job but nothing executes it.

`core/api` is the REST API, built with FastAPI. It is complete at the API layer: every endpoint below exists and is tested against real PostgreSQL, with a recording fake standing in for Kafka. Every failure uses one error format with a stable code and no echoed values; requests are size-limited and unknown fields are rejected; each request carries an id used in responses and logs; and the access log records only method, path, and status. A connection's secret is write-only and never appears in a response, error, log, or the OpenAPI schema.

| Endpoint | Behavior |
|---|---|
| `GET /healthz`, `GET /readyz` | Liveness, and readiness (Postgres reachable and schema current; 503 otherwise) |
| `POST /v1/metrics/validate` | Stateless dry run: checks a template, returns its content hash and the SQL and bound parameters a worker would run; touches no database |
| `POST /v1/connections`, `GET /v1/connections/{id}` | Registers a connection (config checked, never connected to; secret encrypted) and reads it back without the secret. Connections are immutable |
| `POST /v1/metrics`, `POST /v1/metrics/{id}/versions`, `GET /v1/metrics/{id}/versions/{n}` | Registers a metric and its immutable versions; identical content returns the existing version; a duplicate name is a 409 |
| `POST /v1/engines`, `POST /v1/engines/{id}/versions`, `GET /v1/engines/{id}/versions/{n}` | The same for engines; the engine's declaration is validated |
| `POST /v1/jobs` | Requires an `Idempotency-Key` header. Validates parameters, engine support for the metric type, and engine parameters before storing anything, commits the job as `queued`, then publishes its id. Answers **202**. The same key and same request returns the original job (header `Idempotent-Replayed: true`, no second publish); the same key with a different request is a 409 |
| `GET /v1/jobs/{id}` | Job status |
| `GET /v1/jobs/{id}/result` | The result with its traceability record; 409 `result_not_ready` while `queued` or `running`, 409 `job_failed` after a failure |

**What this is and is not.** A 202 means the job is recorded in Postgres, not that anything is running it. The API stores jobs and answers questions about them; it never runs an analysis or connects to a user's database. There is no worker yet, so a submitted job stays `queued`. This is the implemented API, not an operating distributed system.

**Not wired yet:**
- **The real Kafka publisher.** Only the `JobPublisher` interface exists; tests use a recording fake. With no publisher configured, a submit still stores the job. Component 7's local Kafka comes first.
- **A database-backed Uvicorn configuration.** `create_app` accepts a session factory, but nothing builds one from configuration, so the run command below serves only the stateless endpoints and `/readyz` returns 503.
- **Recovery from the dual-write gap (Q1).** If the process dies, or publishing fails, after the job is committed, the job stays `queued` with no message. This is deliberately visible and documented; the recovery mechanism is decided in Component 6.

**Security assumption: trusted network only.** V1 has no authentication, authorization, or rate limiting (Q15). Anyone who can reach the API can register metrics and connections, submit jobs, and read results. Do not expose it beyond a trusted network.

With a database configured, `create_app` validates `STATLOOM_SECRET_KEY` at startup and refuses to start if it is missing or malformed (it names the variable, never the key).

The API is covered by 63 invariant tests (39 for the foundation and validation endpoint, 24 for registration, connections, and jobs), including the secret never leaving the system, commit-before-publish checked from a second database connection, a failed publish leaving the job stored, and 12 simultaneous identical submits producing one job and one publish. Deliberate breakage of the API (18 mutations in all) confirmed the tests notice.

In total 357 tests pass: 14 database, 39 connector, 65 metric, 72 engine, 63 API, and 104 persistence and security.

### Example: a parameterized metric

A metric is plain SQL in your own dialect with `:name` placeholders and optional `{% if %}` blocks. Values are bound by the driver, never written into the SQL text.

```sql
SELECT variant, revenue AS value
FROM orders
WHERE experiment_id = :experiment_id
  AND order_date >= :start_date
  {% if segment is not none %}AND segment = :segment{% endif %}
```

```python
from core.metrics import ParamSpec, ParamType, render, validate_template

specs = [
    ParamSpec(name="experiment_id", type=ParamType.STRING),
    ParamSpec(name="start_date", type=ParamType.DATE),
    ParamSpec(name="segment", type=ParamType.STRING, required=False),
]
validate_template(sql, specs)                      # at registration
q = render(sql, specs, {"experiment_id": "exp_42", "start_date": "2025-01-01"})
# q.sql has no segment filter; q.params holds the bound values
```

The query must return a `variant` and a `value` column (optional `unit_id`). Without a segment, the filter and its parameter are dropped.

### Example: running an engine

Every engine consumes the same validated input and returns the same validated output. The runner checks the data going in and the result coming out.

```python
from core.engines import PinnedEngine, create_engine, run_engine, to_json
from core.metrics import MetricType

rows = [("control", 10.0), ("control", 12.0), ("control", 11.5),
        ("treatment", 13.0), ("treatment", 14.5), ("treatment", 12.5)]
engine = create_engine("reference")
result = run_engine(engine, PinnedEngine("reference", engine.version), MetricType.CONTINUOUS,
                    ("variant", "value"), rows, control_variant="control")
print(to_json(result))   # effect, standard error, confidence interval, p-value
```

A custom engine is a class that declares its name, version, supported metric types, and parameters, implements `run`, and is registered with `@register("name")`. No core code changes.

## Getting Started

**Prerequisites:** Python 3.11+ and a local PostgreSQL installation (`initdb` and `pg_ctl` on your `PATH`), or a Postgres server you can create and drop databases on.

```bash
git clone https://github.com/faithscript/statloom.git
cd statloom

python3 -m venv .venv
.venv/bin/pip install "sqlalchemy>=2" "psycopg[binary]>=3" alembic pydantic jinja2 numpy scipy cryptography fastapi uvicorn httpx pytest

# run all tests (starts and stops a throwaway local Postgres automatically)
.venv/bin/pytest -q

# run one test file or one test
.venv/bin/pytest tests/invariants/test_db.py -v
.venv/bin/pytest tests/invariants/test_db.py -k lock_row -v
.venv/bin/pytest tests/invariants/test_connectors.py -v
.venv/bin/pytest tests/invariants/test_metrics.py -v
.venv/bin/pytest tests/invariants/test_engines.py -v
.venv/bin/pytest tests/invariants/test_api.py -v
.venv/bin/pytest tests/invariants/test_models.py tests/invariants/test_jobs.py -v   # database rules and the job lifecycle
.venv/bin/pytest tests/invariants/test_secrets.py -v

# or use your own Postgres
TEST_PG_URL="postgresql+psycopg://user:pass@localhost:5432/postgres" .venv/bin/pytest -q
```

If no Postgres is found and `TEST_PG_URL` is unset, the tests skip rather than fail.

To create the schema in your own database and encrypt connection credentials, set a database URL and a Fernet key (several comma-separated keys allow rotation; the newest goes first):

```bash
export DATABASE_URL="postgresql+psycopg://user:pass@localhost:5432/statloom"
export STATLOOM_SECRET_KEY="$(.venv/bin/python -c 'from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())')"
.venv/bin/alembic upgrade head
```

The tests set their own key and databases.

### Try the API

The stateless endpoints work without a database:

```bash
.venv/bin/python -m uvicorn core.api:create_app --factory --no-access-log

curl -s localhost:8000/healthz
curl -s -X POST localhost:8000/v1/metrics/validate -H 'content-type: application/json' -d '{
  "sql_template": "SELECT variant, revenue AS value FROM orders WHERE experiment_id = :experiment_id",
  "metric_type": "continuous",
  "parameters": [{"name": "experiment_id", "type": "string"}],
  "sample_values": {"experiment_id": "exp_42"}
}'
```

`/readyz` returns 503 until the app is given a database. The registration and job endpoints are registered only when the app has a database, and that wiring (plus logging configuration) is pending work.

## Roadmap

V1 is deliberately narrow. Possible V2 directions, not commitments:

- React dashboard
- Scheduled analyses
- Additional database providers
- Kafka-based worker autoscaling
- Advanced statistical methods
- Dedicated analytical warehouse and production-grade observability
- Multi-tenancy and sandboxing of untrusted engines
- Experiment assignment and feature flag management

## Documentation

- [Project specification](version.md): original V1 scope and acceptance criteria
