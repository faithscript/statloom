# Component 1 Plan: `core/db`

**Status: ✅ built, 14 tests passing.** Where the build differed from the original plan, the text below describes what was actually built.

## Files
- `core/db/session.py`
- `core/db/base.py`
- `core/migrations/` (`env.py`, `versions/0001_trigger_functions.py`) and `alembic.ini`
- `tests/invariants/test_db.py` and shared fixtures in `tests/conftest.py`

## `session.py`

| Function | Purpose | Invariants enforced |
|---|---|---|
| `get_engine(url)` | Build the SQLAlchemy engine once (cached). Connect args set session timezone to UTC. | All timestamps are UTC |
| `get_session_factory(engine)` | `sessionmaker` with `expire_on_commit=False` and `autoflush=False`. | Writes happen only at explicit points, so no hidden partial state |
| `session_scope(factory=None)` | Context manager: yield a session, commit on success, roll back on any exception (including `KeyboardInterrupt`), always close. The only supported way to open a transaction. The factory is optional and defaults to the one for `$DATABASE_URL`. | Multi-write operations are atomic (result + job `succeeded`, version allocation) |
| `lock_row(session, model, id)` | `SELECT ... FOR UPDATE` helper for services that must serialize. Raises `LookupError` if the row doesn't exist. | Gapless version numbers under concurrency; single job claimer |
| `check_schema_current(engine)` | Compare the DB's Alembic head to the code's head; raises `SchemaMismatchError` on mismatch (DB behind or ahead). | Schema changes come only through migrations |

## `base.py`

| Item | Purpose | Invariants enforced |
|---|---|---|
| `Base` | Declarative base with a naming convention for constraints and indexes, so migrations are deterministic and constraint names are stable. | Migrated schema equals model schema |
| `TimestampMixin` | `created_at` and `updated_at` columns with `server_default=now()`. Both are timezone-aware and `NOT NULL`. | DB-set UTC timestamps |
| `touch_updated_at` trigger (SQL, created in the first migration, attached per table) | Sets `updated_at = now()` on UPDATE. Never lets it move backward (`GREATEST(old, now())`). | `updated_at` monotonic |
| `created_at` guard trigger | Rejects any UPDATE that changes `created_at`. | `created_at` immutable |
| `forbid_mutation` trigger function | Reusable function raising on UPDATE/DELETE. Attached to version tables by their models later. | Published versions are immutable |
| `attach_timestamp_triggers(table)`, `attach_forbid_mutation(table)` *(added during the build, not in the original plan)* | Return the DDL that attaches the trigger functions to a table. Future migrations call these instead of repeating the SQL. | Same invariants as the triggers above, applied per table |

## Test Plan (`tests/invariants/test_db.py`)
- Session timezone is UTC, and inserted timestamps come back tz-aware UTC.
- `session_scope` rolls back both writes when the second of two raises; commits both otherwise.
- Two concurrent `lock_row` calls on the same row serialize (second blocks until first commits).
- `check_schema_current` raises when the DB is behind or ahead of the code head.
- Updating a row bumps `updated_at`, never decreases it, and an attempted `created_at` change raises.
- `forbid_mutation` raises on UPDATE and DELETE for a table it is attached to.
- Alembic autogenerate against a freshly migrated DB produces an empty diff.

## Build Order
1. `base.py` (Base, naming convention, mixin).
2. First migration: the trigger functions (`touch_updated_at`, `created_at` guard, `forbid_mutation`).
3. `session.py` (engine, factory, `session_scope`, `lock_row`, `check_schema_current`).
4. Invariant tests, run against a real Postgres, not SQLite, since triggers and `FOR UPDATE` are Postgres behavior. In practice the fixture starts a throwaway local cluster with `initdb`/`pg_ctl` (Docker was not used), or uses your own server via `TEST_PG_URL`.

## Notes
- The trigger functions live in migrations, not Python, so the DB guarantees them even if code is bypassed.
- Services must never create sessions directly; they receive one from `session_scope`.
- `now()` is the transaction's start time, so two updates in one transaction share a timestamp. `updated_at` is non-decreasing, not strictly increasing.
- Each test gets its own freshly created database, so tests cannot affect each other.

---

## Navigation Helper (implemented: `core/db`)

Each implemented file opens with a docstring containing its own navigation list. Status: all `core/db` items below are built and tested (14 passing).

### Key functions and locations

| Symbol | Location | Enforces |
|---|---|---|
| `NAMING_CONVENTION` | [core/db/base.py:20](../core/db/base.py#L20) | Migrated schema = model schema |
| `Base` | [core/db/base.py:29](../core/db/base.py#L29) | Alembic diff source |
| `TimestampMixin` | [core/db/base.py:35](../core/db/base.py#L35) | DB-set UTC timestamps |
| `attach_timestamp_triggers` | [core/db/base.py:51](../core/db/base.py#L51) | Monotonic `updated_at`, immutable `created_at` |
| `attach_forbid_mutation` | [core/db/base.py:65](../core/db/base.py#L65) | Immutable version rows |
| `get_engine` | [core/db/session.py:36](../core/db/session.py#L36) | UTC connections |
| `get_session_factory` | [core/db/session.py:51](../core/db/session.py#L51) | No hidden partial writes |
| `session_scope` | [core/db/session.py:63](../core/db/session.py#L63) | Atomic multi-write transactions |
| `lock_row` | [core/db/session.py:82](../core/db/session.py#L82) | Gapless versions, single claimer |
| `check_schema_current` | [core/db/session.py:95](../core/db/session.py#L95) | Schema changes only via migrations |
| `SchemaMismatchError` | [core/db/session.py:30](../core/db/session.py#L30) | Raised by the check above |
| `touch_updated_at` | [0001_trigger_functions.py:24](../core/migrations/versions/0001_trigger_functions.py#L24) | Monotonic `updated_at` |
| `guard_created_at` | [0001_trigger_functions.py:32](../core/migrations/versions/0001_trigger_functions.py#L32) | Immutable `created_at` |
| `forbid_mutation` | [0001_trigger_functions.py:42](../core/migrations/versions/0001_trigger_functions.py#L42) | Append-only tables |

### Where to look for what
- **Writing a new table:** inherit `Base` + `TimestampMixin`, then in its migration call `attach_timestamp_triggers` (and `attach_forbid_mutation` for version tables).
- **Writing a service:** open work with `session_scope`; serialize with `lock_row`.
- **Changing the schema:** add a file under `core/migrations/versions/`.
- **Proving an invariant:** [tests/invariants/test_db.py](../tests/invariants/test_db.py), grouped by invariant. Fixtures are in [tests/conftest.py](../tests/conftest.py).

### Running the tests

Run from the repo root (`/Users/faithanyanwu/statloom`).

```bash
# one-time setup
python3 -m venv .venv
.venv/bin/pip install "sqlalchemy>=2" "psycopg[binary]>=3" alembic pydantic pytest

# run all tests, both components (starts and stops a throwaway local Postgres automatically; needs initdb on PATH)
.venv/bin/pytest -q

# this component only, or one test
.venv/bin/pytest tests/invariants/test_db.py -v
.venv/bin/pytest tests/invariants/test_db.py -k lock_row -v

# use your own Postgres instead (needs permission to CREATE/DROP databases)
TEST_PG_URL="postgresql+psycopg://user:pass@localhost:5432/postgres" .venv/bin/pytest -q
```

If no Postgres is found and `TEST_PG_URL` is unset, the tests skip rather than fail.
