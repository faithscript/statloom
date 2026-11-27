# Statloom

Statloom is a distributed platform for running statistical analyses. Bring your own database, SQL queries, and Python engine, and Statloom handles data retrieval, job execution, and storing results.

> Under active development. See [Development Status](#development-status).

## Motivation

Teams often have their own databases, metric definitions, and statistical methods but still need infrastructure to execute analyses concurrently, distribute workloads, handle failures and retries, scale as demand grows, and store results.

Statloom separates this infrastructure from the analysis itself, allowing teams to focus on what they measure and how they measure it without having to build and maintain their own distributed execution system.

## Core Capabilities

*Planned for V1 unless marked otherwise.*

- **Database extensibility:** a connector interface with PostgreSQL as the first provider and read-only, encrypted credentials.
- **Metrics as code:** versioned, parameterized SQL templates written against the user's own schema.
- **Pluggable statistical engines:** custom Python engines behind a standard input/output contract, plus a reference engine for continuous and proportional metrics.
- **Distributed execution:** asynchronous jobs through Kafka, concurrent workers on Kubernetes, with retries and duplicate-safe processing.
- **Traceable analysis:** every result traces back to the exact metric version, engine version, connection, and parameters that produced it.

## System Architecture

```mermaid
flowchart LR
    User([User / Client]) -->|submit job, poll status| API[REST API]
    API -->|create job| PG[(Statloom Postgres<br/>jobs, results, registries)]
    API -->|publish| K[[Kafka]]
    K --> W1[Worker]
    K --> W2[Worker]
    W1 & W2 -->|run metric SQL, read-only| UDB[(User database)]
    W1 & W2 -->|execute| ENG[Statistical engine]
    W1 & W2 -->|persist result + status| PG
```

Workers claim jobs, fetch metric data through a connector, run the selected engine, and write the result and job status in a single transaction.

## Design Principles

- **Modularity:** database-specific logic, metric definitions, and statistical engines are swappable without changing core orchestration.
- **Correctness:** behavior is defined by explicit, testable invariants (see [v1/v1.md](v1/v1.md)).
- **Database-enforced invariants:** constraints, triggers, and foreign keys come first. Application services come second and handle races.
- **Fault tolerance:** retries are safe, duplicates cannot produce duplicate results, and crashed workers do not strand jobs.
- **Separation of concerns:** the API, workers, connectors, and engines interact only through defined interfaces.

## Technology Stack

| Area | Choice | Responsibility |
|---|---|---|
| Language | Python | Core services and statistical engines |
| API | FastAPI, Pydantic | REST interface, request and engine contracts |
| Persistence | PostgreSQL, SQLAlchemy 2.x, Alembic | Jobs, results, registries, migrations |
| Messaging | Kafka (Redpanda locally) | Asynchronous job distribution |
| Statistics | NumPy, SciPy, pandas | Reference engine |
| Secrets | `cryptography` (Fernet) | Encrypted connection credentials |
| Packaging | Docker | One image per service |
| Orchestration | Kubernetes (local cluster) | Worker replicas and recovery |

SQLAlchemy, psycopg, Alembic, Pydantic, and pytest are in use today. The rest is planned.

## Development Status

### V1 components

| # | Component | Status |
|---|---|---|
| 1 | Core data model | 🚧 In progress: `core/db` done; models, services, and security pending |
| 2 | Database connector layer | ✅ Built: interface, registry, and PostgreSQL provider |
| 3 | Metric registry | ⬜ Planned |
| 4 | Statistical engine interface and reference engine | ⬜ Planned |
| 5 | REST API | ⬜ Planned |
| 6 | Worker service | ⬜ Planned |
| 7 | Infrastructure (Docker, Kubernetes, logging) | ⬜ Planned |
| 8 | Reference implementation and demo | ⬜ Planned |

### Implemented so far

`core/db` provides the transaction boundary, row locking, timestamp and append-only triggers, and a startup schema check. It is covered by 14 invariant tests that run against a real PostgreSQL instance. Details are in the [Component 1 docs](v1/component1plan.md).

`core/connectors` provides a provider-neutral connector interface, a registry, and a read-only PostgreSQL connector with query timeouts, a row cap, bound parameters, and typed, retry-classified errors. It is covered by 39 invariant tests, including a contract suite that runs the same checks against a fake provider and the Postgres one. Details are in the [Component 2 docs](v1/component2plan.md).

## Getting Started

**Prerequisites:** Python 3.11+ and a local PostgreSQL installation (`initdb` and `pg_ctl` on your `PATH`), or a Postgres server you can create and drop databases on.

```bash
git clone https://github.com/faithscript/statloom.git
cd statloom

python3 -m venv .venv
.venv/bin/pip install "sqlalchemy>=2" "psycopg[binary]>=3" alembic pydantic pytest

# run all tests (starts and stops a throwaway local Postgres automatically)
.venv/bin/pytest -q

# run one test file or one test
.venv/bin/pytest tests/invariants/test_db.py -v
.venv/bin/pytest tests/invariants/test_db.py -k lock_row -v
.venv/bin/pytest tests/invariants/test_connectors.py -v

# or use your own Postgres
TEST_PG_URL="postgresql+psycopg://user:pass@localhost:5432/postgres" .venv/bin/pytest -q
```

If no Postgres is found and `TEST_PG_URL` is unset, the tests skip rather than fail.

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
- [V1 components and invariants](v1/v1.md)
- [Component 1 architecture](v1/component1.md): file layout and which invariants each file enforces
- [Component 1 plan](v1/component1plan.md): `core/db` plan, navigation helper, and test commands
- [Component 2 architecture](v1/component2.md): connector invariants, file layout, and which invariants each file enforces
- [Component 2 plan](v1/component2plan.md): `core/connectors` function tables and test plan
