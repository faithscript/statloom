# Component 1: Core Data Model — File Architecture

**Status: 🚧 in progress.** ✅ = built and tested, ⬜ = planned, not built yet.

```
core/
├── db/
│   ├── session.py        # ✅ engine + session factory, UTC enforcement
│   └── base.py           # ✅ declarative base, shared mixins (timestamps), trigger-attach helpers
├── models/               # ⬜ not built yet
│   ├── connection.py
│   ├── metric.py         # metric + metric_version
│   ├── engine.py         # engine + engine_version
│   ├── job.py
│   └── result.py
├── schemas/              # ⬜ Pydantic: validated shapes in/out of the model layer
│   ├── job.py
│   └── ...
├── services/             # ⬜ not built yet
│   ├── versioning.py     # register new immutable versions
│   ├── job_submission.py # create job, pin versions, compute hash
│   ├── job_state.py      # state machine, claim, retry, lease
│   └── result_store.py   # atomic result write + job success
├── security/             # ⬜ not built yet
│   └── secrets.py        # encrypt/decrypt connection credentials
└── migrations/           # ✅ Alembic (0001: trigger functions)
tests/
└── invariants/           # ✅ test_db.py so far; one test file per service/model group
```

## Dependency Flow

`services` → `models` + `schemas` + `security` → `db`. Nothing in `models` or `db` imports from `services`. Callers (API, worker) go through `services` only and never touch models directly.

## Roles and Invariants Enforced

| File | Role | Enforces |
|---|---|---|
| `db/session.py` | Session and transaction boundary, UTC timezone | UTC timestamps; atomic multi-write transactions |
| `db/base.py` | Shared `created_at`/`updated_at` mixin, DB-set defaults | `created_at` immutable, `updated_at` monotonic |
| `models/connection.py` | Connection table; stores only `encrypted_secret` | No plaintext credentials |
| `models/metric.py`, `models/engine.py` | Entity + version tables, `UNIQUE(entity_id, version_number)`, DB trigger blocking UPDATE/DELETE on versions | Immutable versions, no duplicate version numbers, `ON DELETE RESTRICT` |
| `models/job.py` | Pinned non-null FKs, `idempotency_key` unique, `status` check constraint, `attempt_count <= max_attempts` check | Pinned versions, resubmission dedup, valid status values, retry cap |
| `models/result.py` | `UNIQUE(job_id)`, non-null FK to job | One result per job, traceability |
| `schemas/*` | Validate parameters and API-safe shapes (credentials excluded) | Validated JSONB params, no secret leakage |
| `services/versioning.py` | Allocates the next version number under a row lock | Gapless version numbers under concurrency |
| `services/job_submission.py` | Pins versions, computes config hash, upserts on `idempotency_key` | Pinned config, same inputs → same hash, idempotent submit |
| `services/job_state.py` | Compare-and-set transitions (`WHERE status = expected`), lease/heartbeat reclaim, retry increment | Legal state machine only, single claimer, stranded-job recovery, retry cap |
| `services/result_store.py` | One transaction: insert result + mark `succeeded` | Result ⇔ succeeded; no duplicate results on retry |
| `security/secrets.py` | Fernet encrypt/decrypt; decrypt only at connector use | Credentials never in responses, logs, or job records |
| `migrations/` | Sole schema change path | Schema matches models (checked in CI) |

## Layering Principle

Invariants are enforced in the database first (constraints, triggers, FKs) and services second. Services provide the correct API and handle races, while the DB guarantees correctness even if a service has a bug.
