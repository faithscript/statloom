# Component 2: Database Connector Layer — File Architecture

**Status: ✅ built, 39 tests passing.**

Connectors talk to **user databases** (untrusted, read-only, many dialects). They are separate from `core/db`, which is Statloom's own Postgres.

## Invariants

**Explicit requirements**
- **Read-only:** no write, DDL, or session change can succeed through a connector. Verified by trying INSERT, CREATE, DROP, `SET ... read_only = off`, and multi-statement payloads.
- **Credentials stay secret:** the secret is held only in memory, as a masked type. It never appears in `repr`, exception messages, or logs.
- **Orchestration independence:** core job code imports only the connector interface and registry, never a provider module or provider types.
- **Pluggable:** a new provider registers without changing core code. A fake connector passes through the same path as Postgres.
- **Uniform results:** every provider returns the same `QueryResult` shape (columns, rows of Python-native values).
- **Bound parameters only:** parameter values are sent as bound values and never interpolated into SQL text.

**Implied safeguards**
- **Bounded execution:** every query has a timeout. Exceeding it raises `QueryTimeout`.
- **No silent partial data:** a result over `max_rows` raises `ResultTooLarge` instead of truncating, since truncated rows would silently skew statistics.
- **No leaked connections:** connections are closed on success, error, and timeout.
- **Typed, classified errors:** every failure is a `ConnectorError` marked transient or permanent, so retry logic (Component 6) can decide without parsing driver messages.
- **Registry is strict:** duplicate type names are rejected and unknown types raise a clear error.
- **Config is validated:** connection config is checked before use, and unknown fields are rejected.

## Layout

```
core/connectors/
├── errors.py       # ConnectorError hierarchy (transient vs permanent), redaction
├── base.py         # ConnectionSpec, QueryResult, Connector interface
├── registry.py     # type name -> connector class
├── postgres.py     # PostgresConfig + PostgresConnector (first provider)
└── __init__.py     # public surface: Connector, ConnectionSpec, QueryResult, create_connector
tests/
├── fakes.py                  # in-memory FakeConnector for contract and independence tests
└── invariants/test_connectors.py
```

## Dependency Flow

`worker / services` → `registry` → `base` + `errors`. `postgres.py` → `base` + `errors` + `psycopg`. Providers register themselves with the registry.

Connectors **never import** `core.db`, `core.models`, or `core.services` (an import-scan test enforces this). They receive a plain `ConnectionSpec` (type, config, secret). A service in Component 1 is meant to build the spec from a stored connection record and decrypt the secret, so the connector layer stays free of Statloom's persistence. **That service is not built yet**, so nothing creates a `ConnectionSpec` from a stored connection today.

## Roles and Invariants Enforced

| File | Role | Enforces |
|---|---|---|
| `errors.py` | Error hierarchy with a `transient` flag; `redact()` scrubs secrets from messages | Typed, classified errors; credentials never in exceptions |
| `base.py` → `ConnectionSpec` | Holds type, config dict, secret as a masked `SecretStr` | Credentials stay secret (`repr` is masked) |
| `base.py` → `QueryResult` | Immutable columns + rows container | Uniform results across providers |
| `base.py` → `Connector` | Abstract interface: open, close, `run_query(sql, params, timeout, max_rows)`, `validate`; context-manager protocol | Orchestration independence; no leaked connections; bound-params-only signature |
| `registry.py` | `register`, `get_connector_class`, `create_connector` | Pluggable; strict registry |
| `postgres.py` → `PostgresConfig` | Validated config (host, port, db, user), unknown fields rejected | Config is validated |
| `postgres.py` → `PostgresConnector` | Read-only session, `statement_timeout`, server-side binding, `max_rows` check, driver-error translation | Read-only; bounded execution; no silent partial data; bound params; typed errors |
| `tests/fakes.py` | Second provider used only in tests | Proves pluggable and independent |
| `tests/invariants/test_connectors.py` | Contract suite run against every registered connector | All of the above |

## Layering Principle

Defense in depth for read-only, three layers: (1) a read-only database role, recommended in the connection docs but not something we can force; (2) a read-only session (`default_transaction_read_only=on` and `READ ONLY` transactions); (3) every query runs inside a `DECLARE ... CURSOR`, which accepts exactly one SELECT-like statement and so rejects multi-statement and DDL/DML payloads. Application-side SQL keyword filtering is never relied on, since it is easy to bypass.
