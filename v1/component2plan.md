# Component 2 Plan: `core/connectors`

**Status: ✅ built, 39 tests passing.** Where the build differed from the original plan, the text below describes what was actually built.

## Files
- `core/connectors/__init__.py` (public surface; imports `postgres` so it registers itself)
- `core/connectors/errors.py`
- `core/connectors/base.py`
- `core/connectors/registry.py`
- `core/connectors/postgres.py`
- `tests/fakes.py`, `tests/invariants/test_connectors.py`

## `errors.py`

| Item | Purpose | Invariants enforced |
|---|---|---|
| `ConnectorError(message, transient)` | Base class for every connector failure. `transient=True` means a retry may succeed. | Typed, classified errors |
| `ConnectionFailed` (transient), `QueryTimeout` (transient) | Network, availability, and timeout failures. | Bounded execution; retry decisions without parsing driver text |
| `AuthenticationFailed`, `QueryRejected`, `ResultTooLarge`, `ConfigInvalid`, `UnknownConnectorType` (permanent) | Failures a retry cannot fix. | Typed errors; no silent partial data; strict registry/config |
| `redact(text, secret)` | Replace any occurrence of the secret with `***`. Applied to every message before raising. | Credentials never in exceptions |

## `base.py`

| Item | Purpose | Invariants enforced |
|---|---|---|
| `ConnectionSpec` | Frozen Pydantic model: `type`, `config: dict`, `secret: SecretStr`. | Credentials stay secret: masked `repr`/`str` |
| `QueryResult` | Frozen: `columns: tuple[str]`, `rows: tuple[tuple]`; `row_count` is a property computed from `rows`. | Uniform results |
| `Connector.__init__(spec)` | Validates config via the provider's config model; stores the spec. Does not connect. | Config is validated |
| `Connector.open()` / `close()` | Acquire and release the connection. `close()` is idempotent and never raises. | No leaked connections |
| `Connector.is_open` *(abstract property, added during the build)* | Whether a live connection is held. Used by `validate()`, the context manager, and tests. | No leaked connections |
| `Connector.__enter__` / `__exit__` | Open on enter, always close on exit. | No leaked connections (success, error, timeout) |
| `Connector.run_query(sql, params=None, *, timeout_s, max_rows)` *(abstract)* | Run one parameterized, read-only query. SQL uses `:name` placeholders; values arrive only via `params`. | Bound parameters only; bounded execution; no silent partial data |
| `Connector.validate()` *(abstract)* | Confirm read-only, run a trivial query. Opens a connection only if none is open and closes it afterwards. Returns or raises a typed error. | Read-only (verified, not assumed) |

## `registry.py`

| Function | Purpose | Invariants enforced |
|---|---|---|
| `register(type_name)` | Class decorator adding a connector class to the registry. Rejects duplicates with `ConfigInvalid`. | Strict registry; pluggable |
| `get_connector_class(type_name)` | Lookup; raises `UnknownConnectorType`. | Strict registry |
| `create_connector(spec)` | Look up by `spec.type` and instantiate. The only entry point core code uses. | Orchestration independence |
| `registered_types()` | List type names (used by the contract suite and docs). | Pluggable |
| `unregister(type_name)` *(added during the build)* | Remove a provider. Used by tests to clean up the fake provider. | Pluggable (test isolation) |

## `postgres.py`

| Function | Purpose | Invariants enforced |
|---|---|---|
| `PostgresConfig` | Pydantic model: host, port, database, user, optional sslmode. `extra="forbid"`. | Config is validated |
| `PostgresConnector.open()` | Connect with session options `default_transaction_read_only=on` and `timezone=UTC`, set the connection read-only (`READ ONLY` transactions), plus `connect_timeout` and `application_name='statloom-connector'` (the leak test counts sessions by this name). | Read-only |
| `translate_params(sql)` *(public, returns the SQL and the names used)* | Convert `:name` placeholders to driver binds. Skips string literals, quoted identifiers, comments, dollar-quoted bodies, and `::` casts; doubles literal `%`. Values are never formatted into the text. | Bound parameters only |
| `PostgresConnector.run_query(...)` | Set `statement_timeout` for the transaction with `set_config`, then run inside a server-side `DECLARE ... CURSOR` (one SELECT-like statement only, so multi-statement and DDL/DML are rejected). `fetchmany(max_rows + 1)`; raise `ResultTooLarge` if over. A missing parameter raises `QueryRejected`; extra parameters are ignored. Always rolls back afterward. Returns a `QueryResult` with Python-native values. | Read-only (layer 3); bounded execution; no silent partial data; bound parameters; uniform results |
| `PostgresConnector.validate()` | Run `SHOW transaction_read_only` and `SELECT 1`; fail with `QueryRejected` unless the session is read-only. | Read-only verified |
| `_translate_error(exc)` *(a method, since it needs the secret)* | Map psycopg exceptions to the `errors.py` types, redacting the secret. Authentication failures are also detected from the message text. | Typed errors; credentials never in exceptions |

## Test Plan (`tests/invariants/test_connectors.py`)

39 tests against a real Postgres standing in as the "user database" (a role that *can* write, so the tests prove the connector, not the role).

- **Read-only:** INSERT, UPDATE, DELETE, CREATE TABLE, DROP TABLE, `SET default_transaction_read_only = off`, and `SET TRANSACTION READ WRITE` are all rejected (`QueryRejected`), and the table is unchanged afterward.
- **Session layer on its own (white-box):** with the cursor guard out of the picture, flipping `default_transaction_read_only` off and then INSERTing still fails with `ReadOnlySqlTransaction`, because the transaction itself is `READ ONLY`.
- **Multi-statement:** `SELECT 1; SET ...; COMMIT; DROP TABLE t` is rejected and `t` still has its rows.
- **Credentials:** `repr`/`str` of the spec and the connector hide the secret. A wrong password raises `AuthenticationFailed` (permanent), and neither the message nor the captured logs contain it. A correct password connects. *(These two use the local cluster's password-protected `app_user` and skip when `TEST_PG_URL` is set.)*
- **Bound parameters:** an injection payload stored as a row's value is matched literally with no side effect. `::int` casts, a `:` inside a string literal, and `%` in a `LIKE` all still work. A missing parameter is rejected.
- **Timeout:** `pg_sleep(5)` with `timeout_s=1` raises `QueryTimeout` (transient), returns in under 4 seconds, and the connector is still usable afterward.
- **Row cap:** a result over `max_rows` raises `ResultTooLarge`; a result exactly at the cap is returned whole.
- **No leaks:** after success, error, and timeout, `pg_stat_activity` shows no session with `application_name='statloom-connector'`. A second `close()` is a no-op.
- **Error classes:** an unreachable host is `ConnectionFailed` (transient); an unknown column is `QueryRejected` (permanent).
- **Registry:** an unknown type raises `UnknownConnectorType`; a duplicate registration raises `ConfigInvalid`.
- **Config:** a missing host, an unknown field, and an out-of-range port raise `ConfigInvalid`, and the message never echoes the offending input value.
- **Pluggable and uniform (contract suite):** the same checks run on both `FakeConnector` and `PostgresConnector`: identical `QueryResult` shape, row cap, clean idempotent close, and `validate()`. The fake is registered at runtime with no core edits.
- **Independence:** an AST import scan asserts `core/connectors` imports nothing from `core.db`, `core.models`, `core.services`, `core.schemas`, or `core.security`.

**Test strength check:** with the read-only session settings and the cursor guard temporarily removed, four tests failed (the white-box session test, the multi-statement test, `validate`, and the Postgres contract `validate`), then passed again once restored. The plain INSERT/DROP cases still passed in that run because the transaction is rolled back after the failed fetch, so those cases alone are not proof of the guards.

## Build Order
1. `errors.py` (hierarchy + `redact`).
2. `base.py` (`ConnectionSpec`, `QueryResult`, `Connector` interface).
3. `registry.py`.
4. `postgres.py` (config, `translate_params`, `open`, `run_query`, `validate`, `_translate_error`) and `__init__.py`.
5. `tests/fakes.py` and `tests/invariants/test_connectors.py`, plus new fixtures in `tests/conftest.py` (a password-protected `app_user` role, a seeded `user_db`).

*Deviation:* the plan interleaved tests with each step; in practice all the code was written first and then the tests, with the contract suite last.

## Notes
- New dependency: `pydantic` (for `SecretStr` and config validation).
- Parameter style is `:name` in the interface so SQL templates (Component 3) are provider-neutral. `translate_params` is now covered by tests (casts, string literals, `%`). **Known gap:** an array slice like `a[x:y]` is mistaken for a placeholder.
- Read-only has three layers: a recommended read-only role, a read-only session, and the single-statement cursor guard. None of them is a security boundary against hostile SQL, which is why V1 trusts the SQL author (untrusted SQL is V2).
- Exceeding `max_rows` fails the job rather than truncating. Truncated data would silently corrupt the statistics.
- Connectors are opened per job and closed after. Pooling is deferred until it's measurably needed.
- **Not built yet:** the Component 1 service that builds a `ConnectionSpec` from a stored connection and decrypts its secret.

---

## Navigation Helper (implemented: `core/connectors`)

Each implemented file opens with a docstring containing its own navigation list.

### Key functions and locations

| Symbol | Location | Enforces |
|---|---|---|
| `ConnectorError` and subclasses | [errors.py:19](../core/connectors/errors.py#L19) | Typed, retry-classified errors |
| `redact` | [errors.py:57](../core/connectors/errors.py#L57) | Credentials never in exceptions |
| `ConnectionSpec` | [base.py:21](../core/connectors/base.py#L21) | Credentials stay secret |
| `QueryResult` | [base.py:36](../core/connectors/base.py#L36) | Uniform results |
| `Connector` | [base.py:47](../core/connectors/base.py#L47) | Orchestration independence; bound-params-only signature |
| `Connector._parse_config` | [base.py:60](../core/connectors/base.py#L60) | Config validated; no input echoed |
| `register`, `unregister` | [registry.py:21](../core/connectors/registry.py#L21), [:31](../core/connectors/registry.py#L31) | Pluggable; strict registry |
| `get_connector_class`, `create_connector` | [registry.py:35](../core/connectors/registry.py#L35), [:44](../core/connectors/registry.py#L44) | Strict registry; the one entry point for core code |
| `PostgresConfig` | [postgres.py:31](../core/connectors/postgres.py#L31) | Validated config |
| `translate_params` | [postgres.py:41](../core/connectors/postgres.py#L41) | Bound parameters only |
| `PostgresConnector.open` | [postgres.py:115](../core/connectors/postgres.py#L115) | Read-only session; the only place the secret is read |
| `PostgresConnector.run_query` | [postgres.py:146](../core/connectors/postgres.py#L146) | Cursor guard, timeout, row cap |
| `PostgresConnector.validate` | [postgres.py:179](../core/connectors/postgres.py#L179) | Read-only verified |
| `PostgresConnector._translate_error` | [postgres.py:199](../core/connectors/postgres.py#L199) | Typed errors; secret redacted |

### Where to look for what
- **Adding a provider:** subclass `Connector`, set `config_model`, decorate with `@register("name")`, and import the module in `core/connectors/__init__.py`. No core changes.
- **Using a connector from core code:** build a `ConnectionSpec`, call `create_connector(spec)`, use it as a `with` block, and catch `ConnectorError` (check `.transient` to decide on a retry).
- **Proving an invariant:** [tests/invariants/test_connectors.py](../tests/invariants/test_connectors.py), grouped by invariant. The fake provider is in [tests/fakes.py](../tests/fakes.py).

### Running the tests

Run from the repo root (`/Users/faithanyanwu/statloom`).

```bash
# one-time setup
python3 -m venv .venv
.venv/bin/pip install "sqlalchemy>=2" "psycopg[binary]>=3" alembic pydantic pytest

# all tests, both components (starts and stops a throwaway local Postgres; needs initdb on PATH)
.venv/bin/pytest -q

# this component only, or a subset by name
.venv/bin/pytest tests/invariants/test_connectors.py -v
.venv/bin/pytest tests/invariants/test_connectors.py -k "rejected or read_only" -v

# use your own Postgres instead (the two password tests will skip)
TEST_PG_URL="postgresql+psycopg://user:pass@localhost:5432/postgres" .venv/bin/pytest -q
```
