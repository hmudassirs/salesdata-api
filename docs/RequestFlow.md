# Request Flow

## Normal protected request

The current request flow for a protected `/api` endpoint is:

```text
ASGI server
  -> FastAPI middleware stack
     -> authentication middleware
        -> API-key validation and optional user lookup
        -> or JWT validation
     -> existing observability middleware
        -> route matching and dependency resolution
           -> DatabaseSession dependency
           -> ServiceManager dependency
           -> CurrentUser dependency
        -> endpoint
           -> optional persistent query-cache lookup
           -> DatabaseSession async pool acquire
           -> thread-offloaded adapter execute/fetch
           -> optional background cache store
        -> response creation
     -> existing request log / trace / audit enqueue
  -> response
```

Middleware registration deliberately makes authentication outermost. An
unauthenticated request can therefore return `401` before the existing
observability middleware executes. This preserves the application’s present
behaviour, but it matters to future performance middleware: an optional
profiler middleware must be placed outside authentication if rejected request
traces are required.

## `/api/query` path

`execute_query` receives `Request`, `QueryRequest`, `DatabaseSession`,
`ServiceManager`, and `CurrentUser` through FastAPI routing and dependency
injection.

1. It builds the existing request context.
2. For `SELECT` statements it derives a cache key and performs the persistent
   SQLite cache lookup in a worker thread.
3. On a cache hit it returns the cached response without acquiring a DuckDB
   connection.
4. On a miss it enters `DatabaseSession.get_async_session()`, which obtains a
   wrapper from `AsyncConnectionPool`.
5. `AsyncSessionWrapper.fetch_all()` uses `asyncio.to_thread` to call the
   synchronous adapter method on the leased connection.
6. The session context releases the wrapper back to the pool.
7. Non-empty `SELECT` results schedule a cache write in the background; that
   write is intentionally not awaited by the response path.
8. The route builds `QueryResponse` and returns it.

## Existing write path

At request completion, the existing observability middleware creates a
request-log, persisted-trace, and audit record. With the normal lifecycle
configuration, the record is enqueued to `ObservabilityWriteQueue`; its
background worker batches writes into SQLite transactions. In reduced test
configurations where the queue is absent, the code falls back to a synchronous
transactional write.

## Performance trace model

The future profiler represents the same request as a request-local tree, not
as a single stopwatch:

```text
REQUEST
├── AUTHENTICATION
│   └── API_KEY_LOOKUP
├── DEPENDENCY
├── CACHE_LOOKUP
├── CONNECTION
│   ├── POOL_WAIT
│   └── POOL_ACQUIRE
├── SQL
│   ├── SQL_EXECUTE
│   └── SQL_FETCH
├── CACHE_STORE
├── SERIALIZE
└── RESPONSE
```

Each node records monotonic nanosecond start and finish values, duration,
parent/children links, tags, and metadata. Inclusive time is the node duration.
Exclusive time is the node duration less the completed child durations. The
tree is request-local and is sampled as a unit, so child events cannot exist
without their request root.

## Instrumentation ownership

The profiler does not alter endpoint, adapter, pool, or authentication
semantics. The tracing engine owns only trace construction and event dispatch.
Optional integration adapters own the decision to surround an existing call.
Exporters consume completed events and never participate in request control
flow.

