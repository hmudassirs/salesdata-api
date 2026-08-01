# Adapters (Phases 5–8)

Every adapter in `core/performance/adapters/` follows the same shape:
wrap an existing class/function behind an identical public interface,
delegate every call — arguments, return value, and exception —
unchanged, and add timing only when `core.performance.context
.get_current_profiler()` returns a bound `RequestProfiler` (see
`configuration.md`'s disabled-path guarantee). None of the wrapped
subsystem files are modified. Adopting an adapter is opt-in at the call
site: swap the object being constructed/passed for its wrapped
equivalent, nothing else changes.

Each adapter's regression tests (`core/performance/tests/test_adapters_*.py`)
assert this directly against the real wrapped subsystem — a real
`SyncConnectionPool`/`AsyncConnectionPool`, real `core.db.transactions`
functions, a real `APIKeyService`/`jwt.decode`, a real DuckDB adapter,
and a real temp-file `ServiceDatabase`/`QueryResultCache` — not a mock,
so a behavioural drift between the adapter and the original would show
up as a failing test, not just a type mismatch.

## `adapters/pool.py` — connection pool

`AsyncPoolTimingAdapter` / `SyncPoolTimingAdapter` wrap
`core.db.pool`'s async/sync connection pools via a structural
`Protocol` (so they work with either pool, a test double, or any future
pool implementation with the same shape — no concrete import needed).

| Method | Stage | Notes |
|---|---|---|
| `acquire()` | `POOL_ACQUIRE` | Also publishes `pool_active_connections`/`pool_idle_connections` gauges from the pool's own `metrics()`. |
| `release(conn)` | `POOL_RELEASE` | Same gauge publication. |
| `close_all()`, `metrics()` | *(untimed)* | Forwarded directly. |

## `adapters/transactions.py` — pool-based transactions

`core.db.transactions.transaction`/`async_transaction` are small
generator-based context managers (acquire → `BEGIN` → yield → `COMMIT`
or `ROLLBACK` → release) not built to be instrumented from the outside
— a `contextmanager`'s internal steps aren't individually observable
through its public `__enter__`/`__exit__`. `instrumented_transaction`/
`instrumented_async_transaction` mirror the original logic exactly —
same acquire/BEGIN/COMMIT/ROLLBACK/release sequence, same exception
propagation, same best-effort-rollback-swallow behaviour — with timers
added around each step:

| Step | Stage |
|---|---|
| `BEGIN` | `TRANSACTION_BEGIN` |
| commit path | `TRANSACTION_COMMIT` |
| rollback path | `TRANSACTION_ROLLBACK` |

## `adapters/auth.py` — authentication

- `InstrumentedAPIKeyService` wraps `APIKeyService`: `validate_api_key`
  is timed under `API_KEY_LOOKUP`; CRUD operations (create/list/revoke)
  are timed under `CUSTOM` (there is no dedicated auth-CRUD stage).
- `instrumented_jwt_decode(token, key, algorithms)` is a drop-in wrapper
  around `jwt.decode` — same positional signature, same return value,
  same `jwt.PyJWTError` (and subclasses) on failure — timed under
  `AUTHENTICATION`.
- `authorize(check, ...)` is a generic helper for timing an arbitrary
  authorization predicate under `AUTHORIZATION`.

## `adapters/duckdb.py` — DuckDB

`InstrumentedDuckDBAdapter` wraps the DuckDB adapter behind a
`Protocol`. `execute`/`insert`/`update`/`delete`/`create`/`drop` are
timed under `SQL_EXECUTE`; `fetch_one`/`fetch_all` under `SQL_FETCH`.
Everything else (connection lifecycle, schema introspection, ...) is
forwarded untimed via `__getattr__`.

## `adapters/sqlite.py` — SQLite service database and query cache

Two independent wrappers, both added in Phase 8:

- `InstrumentedServiceDatabase` wraps `ServiceDatabase`. `execute`,
  `fetch_one`, and `fetch_all` (each of which acquires its own pooled
  connection) are timed under `SQL_EXECUTE`/`SQL_FETCH`; `execute_on`
  and `fetch_one_on` (used against an already-acquired connection
  inside `transaction()`) get the same stages under distinct metric
  names so the two call shapes stay distinguishable in a trace.
  `transaction()` is timed as **one** `TRANSACTION_COMMIT` span
  covering acquire+yield+commit-or-rollback together: unlike
  `core.db.transactions`, `ServiceDatabase.transaction` relies on
  sqlite3's implicit-transaction/`.commit()`/`.rollback()` API rather
  than explicit `BEGIN`/`COMMIT`/`ROLLBACK` statements, so there is no
  separate begin/commit boundary to instrument without duplicating
  `ServiceDatabase`'s private connection-acquisition logic. Everything
  administrative (`connect`, `create_tables`, the user/API-key CRUD
  helpers, `cleanup_expired_cache`, `get_stats`, ...) is forwarded
  untimed via `__getattr__`.
- `InstrumentedQueryResultCache` wraps `QueryResultCache`.
  `get_cached_result` is timed under `CACHE_LOOKUP` and `cache_result`
  under `CACHE_STORE`. `generate_cache_key` (pure, in-memory hashing),
  `invalidate_cache`, and `get_cache_stats` are forwarded untimed.

## Adding a new adapter

The pattern to follow for any subsystem not yet covered:

1. Identify which of its methods are actually on a request's hot path
   (vs. administrative/one-time-per-process calls) and which
   `PerformanceStage` each belongs to.
2. Wrap, don't modify: the new class takes the original instance in its
   constructor and either implements the timed methods explicitly or
   forwards via `__getattr__` for everything else.
3. Check `get_current_profiler()` once per timed method; if `None`,
   delegate directly with no `with profiler.stage(...):` wrapper.
4. Write a regression test against the *real* wrapped subsystem
   (a temp-file database, a real pool, ...), not a mock, asserting the
   adapter's return values/exceptions match the original's exactly, in
   addition to asserting the expected timer names appear when profiled.
