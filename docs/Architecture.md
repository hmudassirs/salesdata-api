# Architecture

## Purpose

`salesdata-api` is a FastAPI application that exposes a data-query API over a
DuckDB warehouse and maintains operational data in a separate SQLite service
database. The codebase is organised by domain and composition boundaries,
rather than by a single monolithic service module.

## Runtime composition

`run_api.py` constructs `ApplicationLifespan`. The lifespan starts ordered
`LifecycleStep` implementations and registers their outputs with
`DependencyContainer`.

- `DataWarehouseStep` constructs `DatabaseSession` for the configured data
  backend. Its normal asynchronous mode creates an `AsyncConnectionPool`.
- `ServiceDatabaseStep` opens `ServiceDatabase`, creates its schema, builds a
  `ServiceManager`, and starts the observability write queue.
- `ServiceManager` is the composition root for user and API-key repositories,
  request logging, request tracing, auditing, and query-result caching.

The application factory in `core.app.api.app` creates the FastAPI app, stores
the long-lived dependencies on `app.state`, installs middleware, and mounts
the database and authentication routers.

## Core domains

| Area | Responsibility | Primary boundary |
| --- | --- | --- |
| `core.app` | Application configuration, lifetime, dependency adapters, health, and API factory | `ApplicationLifespan`, `DependencyContainer` |
| `core.auth` | API-key/JWT enforcement, current-user dependency, password hashing, repositories, and API-key service | `APIKeyService`, `UserRepository`, `APIKeyRepository` |
| `core.caching` | Persistent query-result cache | `QueryResultCache` |
| `core.db` | Adapter registry, database sessions, pool implementations, transactions, and database configuration | `DatabaseSession`, `SyncConnectionPool`, `AsyncConnectionPool` |
| `core.storage` | Pooled SQLite service database and its schema | `ServiceDatabase` |
| `core.observability` | Existing request logs, audit records, persisted tracing, Prometheus metrics, OpenTelemetry, and background writes | `ObservabilityWriteQueue` |
| `core.service_registry` | Composition of services sharing the SQLite service database | `ServiceManager` |
| `experimental` | Prototypes not on the production execution path | Experimental modules only |

## Database architecture

The data warehouse uses `DatabaseSession`, which obtains adapter classes from
the adapter registry. DuckDB and SQLite adapters fit the current
`(path, echo=...)` construction convention. The session creates independent
adapter instances per pooled connection; a pool slot never shares a live
database connection with another slot.

`ServiceDatabase` is a distinct pooled SQLite database for API keys, users,
request logs, persisted traces, cache entries, and audit records. Its WAL
configuration and `ObservabilityWriteQueue` exist to keep SQLite writes out
of the normal request completion path.

## Existing observability

The existing observability package is a production feature and must remain
behaviourally independent from the new profiler. It records request logs,
persisted tracing, audit events, optional Prometheus metrics, and optional
OpenTelemetry spans. In particular, it can enqueue request records to a
background SQLite writer.

`core.performance` will not replace or import this package. It will provide
an in-process tracing and metric model that can later be exported alongside
these systems through explicit adapters.

## Compatibility constraints for the performance subsystem

1. `core.performance` is a peer core package, not an application framework.
   Its foundation imports neither FastAPI nor database, authentication, or
   observability modules.
2. Existing APIs remain unchanged. Integration uses optional wrappers,
   injected collaborators, or middleware adapters at stable boundaries.
3. Instrumentation must be disabled by default until explicitly configured.
   The disabled path performs no clock read, event allocation, serialization,
   logging, exporter call, or lock acquisition.
4. Request-local trace state must not use a process-wide lock. Aggregation and
   exporting occur outside the request path.
5. Existing Prometheus and OpenTelemetry integrations remain optional and
   continue to degrade safely when their packages are absent.

## Safe future instrumentation seams

- FastAPI: an optional middleware package creates and closes request traces.
- Dependencies: wrapper utilities can surround existing provider callables
  without changing route signatures.
- Authentication: the API-key service, JWT validation branch, and user-cache
  lookup can be timed by an optional authentication adapter.
- Pools: acquire/release wrappers can measure waiting, lock acquisition, and
  pool state without changing checkout semantics.
- Database sessions/adapters: wrappers can separate SQL execute and fetch
  timing while preserving adapter return values and exceptions.
- Transactions and cache: optional wrappers can record begin/commit/rollback
  and cache lookup/store stages.

No integration work is part of Phase 0.

