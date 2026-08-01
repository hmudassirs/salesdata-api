# API Performance and Latency Reduction Refactor Roadmap

**Project:** salesdata-api / PrepareData API  
**Primary objective:** Reduce API latency and tail latency under high concurrency while preserving correctness, connection safety, cache correctness, and observability.

**Baseline load:** 500 concurrent API requests  
**Current observed result:** 500/500 successful, ~51.8 req/s, average latency ~5.26 s, p50 ~6.05 s, p95 ~8.95 s, p99 ~9.24 s, max ~9.37 s.

**Primary hypothesis:** The current architecture has a severe concurrency bottleneck around the database pool and request pipeline. The application is configured with a small pool (`min_size=2`, `max_size=10`) while the benchmark submits 500 concurrent requests. The performance roadmap therefore prioritizes measuring and eliminating queueing before optimizing the actual SQL execution.

---

## 1. Final Target Architecture

The target request path should become:

```text
HTTP Request
    |
    v
FastAPI / ASGI
    |
    +--> Authentication / API-key lookup
    |        |
    |        +--> in-process auth cache
    |
    v
Request Context
    |
    v
Query Policy / Validation
    |
    +--> normalize SQL + params
    +--> determine read-only vs write
    +--> determine cacheability
    |
    v
Query Cache Coordinator
    |
    +--> L1 in-memory TTL/LRU cache ---- HIT ---> return
    |
    +--> Single-flight registry
    |        |
    |        +--> existing identical query in flight ---> await owner
    |
    +--> L2 persistent cache ------------ HIT ---> populate L1 ---> return
    |
    v
Database Work Scheduler
    |
    +--> bounded DB executor
    |
    +--> connection pool
             |
             +--> idle connection
             |
             +--> create connection if capacity allows
             |
             +--> bounded waiter queue
    |
    v
DuckDB Execution
    |
    +--> prepare
    +--> execute
    +--> fetch
    |
    v
Result
    |
    +--> populate L1 immediately
    |
    +--> enqueue L2 cache persistence asynchronously
    |
    v
Response Serialization
    |
    v
Performance Registry
    |
    +--> cheap in-memory record only
    |
    +--> exporters / persistence out-of-band
```

### Important architectural rules

1. **Do not size the database pool based on HTTP concurrency.**
   The HTTP layer may accept hundreds of concurrent requests while the database pool remains bounded to the database's safe concurrency.

2. **Do not allow 500 identical requests to execute 500 identical database queries.**
   Use cache + request coalescing/single-flight.

3. **Do not use cache-hit traffic to generate synchronous SQLite writes.**
   Cache reads must remain cheap and mostly read-only.

4. **Do not hold the pool lock while performing potentially slow connection creation.**

5. **Do not use one oversized global thread pool for unrelated workloads.**
   Separate database work, service-database work, and background work.

6. **Do not let performance instrumentation perform I/O synchronously on the request path.**

7. **Do not optimize the database query until measurements prove database execution is the bottleneck.**
   First separate queueing latency from execution latency.

8. **Do not use cache keys that can cross authorization or tenant boundaries.**

---

# 2. Roadmap Overview

| Phase | Area | Main outcome | Priority |
|---|---|---|---|
| 0 | Baseline and safety | Reproducible performance baseline | P0 |
| 1 | Performance instrumentation | Exact latency attribution | P0 |
| 2 | Pool observability | Measure contention and queueing | P0 |
| 3 | Pool refactor | Correct acquire/release lifecycle | P0 |
| 4 | Pool sizing | Find optimal concurrency envelope | P0 |
| 5 | Adaptive pool | Adaptive sizing based on real contention | P0 |
| 6 | Executor architecture | Prevent thread-pool contention | P1 |
| 7 | Cache redesign | Fast L1/L2 cache architecture | P0 |
| 8 | Single-flight | Prevent cache stampede | P0 |
| 9 | Query route refactor | Simplify and optimize hot path | P1 |
| 10 | Persistence and observability | Move non-critical I/O out of hot path | P1 |
| 11 | Reliability/security | Preserve correctness under concurrency | P0 |
| 12 | Benchmark and tuning | Prove improvements at 10–500+ concurrency | P0 |
| 13 | Production hardening | Operational limits, alerts, rollback | P1 |

---

# 3. Phase 0 — Establish a Reproducible Baseline

## Goal

Create a stable benchmark environment before changing architecture.

## Tasks

### 0.1 Freeze the current baseline configuration

Record:

- Python version
- OS
- CPU count
- physical/logical cores
- RAM
- DuckDB version
- SQLite version
- FastAPI version
- Uvicorn version
- pool configuration
- executor configuration
- cache configuration
- performance sampling configuration

Record the current database configuration:

```text
min_size = 2
max_size = 10
timeout = 30 seconds
```

Record the server process model:

- one process or multiple
- number of Uvicorn workers
- development or production mode

### 0.2 Run clean baseline tests

Run the same test suite at:

```text
1 concurrent
5 concurrent
10 concurrent
20 concurrent
50 concurrent
100 concurrent
250 concurrent
500 concurrent
```

For every test collect:

- success rate
- wall time
- throughput
- average latency
- p50
- p90
- p95
- p99
- max latency
- error count
- timeout count

### 0.3 Separate workload types

Create at least these benchmark scenarios:

```text
A. SELECT 1, uncached
B. SELECT 1, cached
C. 500 identical concurrent SELECT queries
D. 500 unique SELECT queries
E. small result set
F. medium result set
G. large result set
H. mixed read workload
I. write workload, if writes are allowed
```

### 0.4 Establish a performance budget

Initial targets should be treated as engineering goals, not assumptions:

```text
Cached duplicate query:
    p50 < 5 ms
    p95 < 20 ms

Simple uncached query:
    p50 < 50 ms
    p95 < 200 ms

Heavy query:
    Define a separate workload-specific budget

Pool wait:
    p95 should be near zero for normal load
    p95 should increase only after database capacity is saturated
```

Do not enforce these exact values until the environment is benchmarked. The important goal is to make a clear distinction between:

```text
queueing time
vs
database execution time
vs
serialization time
```

---

# 4. Phase 1 — Complete the Performance Instrumentation

## Goal

Make the performance modules capable of proving exactly where latency is spent.

The existing performance subsystem already supports request-local profiling and a shared in-memory registry. Preserve that architecture.

## 4.1 Keep the hot path cheap

The request performance profiler should:

- use monotonic/high-resolution timestamps
- create request-local stage data
- avoid disk I/O
- avoid SQLite writes
- avoid network calls
- avoid expensive JSON serialization during the request
- avoid logging every request synchronously

The shared registry should remain:

```text
request-local profiler
    ->
in-memory registry
    ->
out-of-band exporter
```

## 4.2 Add or standardize these stages

At minimum:

```text
REQUEST
AUTHENTICATION
API_KEY_LOOKUP
DEPENDENCY
CONTAINER
CACHE_LOOKUP
CACHE_L1_LOOKUP
CACHE_L2_LOOKUP
CACHE_STORE
SINGLE_FLIGHT_WAIT
POOL_WAIT
POOL_ACQUIRE
POOL_RELEASE
DB_EXECUTOR_WAIT
SQL_PREPARE
SQL_EXECUTE
SQL_FETCH
SERIALIZE
RESPONSE
```

## 4.3 Add identifiers/tags

Recommended tags:

```text
endpoint
method
cache_status
cache_layer
query_type
query_hash
pool_size
pool_utilization
executor_name
db_operation
status
error_type
```

Do not put raw SQL in high-cardinality metric labels. Use a stable query hash or query template identifier.

## 4.4 Add latency histograms

For each important stage maintain:

```text
count
mean
min
max
p50
p95
p99
```

Most important:

```text
request_duration
pool_wait_duration
pool_acquire_duration
db_executor_wait
sql_execute_duration
sql_fetch_duration
cache_lookup_duration
single_flight_wait_duration
```

---

# 5. Phase 2 — Add First-Class Pool Contention Metrics

## Goal

Determine whether requests are waiting for a connection or spending time executing SQL.

## Refactor pool metrics

Extend pool metrics to include:

```python
{
    "min_connections": ...,
    "max_connections": ...,
    "current_connections": ...,
    "active_connections": ...,
    "idle_connections": ...,

    "queue_depth": ...,
    "current_waiters": ...,
    "total_waiters": ...,

    "total_acquires": ...,
    "successful_acquires": ...,
    "timed_out_acquires": ...,

    "total_wait_time_ms": ...,
    "avg_wait_time_ms": ...,
    "p50_wait_time_ms": ...,
    "p95_wait_time_ms": ...,
    "p99_wait_time_ms": ...,

    "connection_creation_count": ...,
    "connection_creation_time_ms": ...,

    "utilization": ...,
    "saturation": ...,
}
```

## Required calculations

```text
utilization = active / current_connections

saturation = active / max_connections

queue_depth = number of requests waiting for capacity
```

## Performance dashboard

Expose:

```text
Pool size
Active
Idle
Waiters
Queue depth
Utilization
Saturation
p50 wait
p95 wait
p99 wait
Timeouts
```

## Acceptance criteria

After this phase, a 500-request test must be able to answer:

> "How many milliseconds of the 8.95 second p95 latency were spent waiting for a connection?"

---

# 6. Phase 3 — Refactor the Connection Pool Core

## Goal

Make pool acquisition correct, scalable, measurable, and race-safe.

Likely target area:

```text
core/db/pool/
    async_pool.py
    adaptive.py
    settings/configuration modules
```

## 6.1 Separate pool state from connection creation

Do not do this conceptually:

```text
acquire lock
    |
    +-- create connection while lock is held
```

Instead:

```text
acquire lock
    |
    +-- inspect state
    +-- reserve capacity
    +-- decide action
release lock
    |
    +-- create connection
    |
    +-- reacquire lock
    +-- publish connection
```

## 6.2 Define explicit connection states

Use a consistent state model:

```text
IDLE
IN_USE
CREATING
CLOSING
CLOSED
BROKEN
```

Do not rely on implicit states.

## 6.3 Prevent duplicate creation

Multiple concurrent requests must not all observe:

```text
current_connections < target
```

and independently create connections beyond capacity.

Use explicit reservation accounting:

```text
total = idle + in_use + creating
```

Capacity decisions must use `total`, not just `idle + in_use`.

## 6.4 Fix close/reuse semantics

Guarantee:

```text
acquire
    ->
use
    ->
release
    ->
connection remains usable
```

A closed connection must never be returned to the idle pool.

Validate a connection before reuse when appropriate.

If a connection is broken:

```text
remove from pool
decrement capacity
create replacement only when policy allows
```

Do not lazily recreate connections from a path that assumes a live connection.

## 6.5 Ensure release is exception-safe

Always release with `finally` or a context manager.

Required pattern:

```python
conn = await pool.acquire()
try:
    ...
finally:
    await pool.release(conn)
```

or equivalent context manager.

## 6.6 Add timeout categories

Distinguish:

```text
pool wait timeout
connection creation timeout
query timeout
request timeout
```

Do not report all of them as generic timeout errors.

## 6.7 Add cancellation handling

If a waiting coroutine is cancelled:

- remove its waiter state
- do not leak queue entries
- do not reserve capacity permanently
- do not create a connection for a cancelled request

---

# 7. Phase 4 — Determine the Correct Pool Size

## Goal

Find the optimal pool size experimentally.

Do not jump from:

```text
10 -> 500
```

Run a controlled matrix:

```text
Pool size:
10
20
40
80
120

Concurrency:
10
20
50
100
250
500
```

For every combination capture:

```text
throughput
p50
p95
p99
pool_wait_p95
SQL_execute_p95
CPU
RAM
thread_count
error_rate
```

## Expected interpretation

### Case A

```text
pool_wait high
SQL_execute low
```

Action:

```text
Increase pool or reduce request concurrency.
```

### Case B

```text
pool_wait low
SQL_execute high
```

Action:

```text
Database/query optimization.
```

### Case C

```text
both high
```

Action:

```text
Pool and database are both saturated.
```

### Case D

```text
pool increase makes no improvement
```

Action:

```text
The bottleneck is elsewhere.
```

## Important rule

Choose the pool size from the point of diminishing returns.

Example:

```text
10 -> p95 8.9 s
20 -> p95 4.7 s
40 -> p95 1.8 s
80 -> p95 1.4 s
120 -> p95 1.5 s
```

The optimal size may be approximately 40–80, not 500.

---

# 8. Phase 5 — Replace Hit-Ratio-Based Adaptive Pooling

## Current problem

Pool hit ratio is not a sufficient signal for pool sizing.

The adaptive controller should instead consider:

```text
queue depth
pool utilization
pool saturation
p95 pool wait
timeout rate
connection creation latency
```

## 8.1 New adaptive controller inputs

Recommended:

```text
queue_depth
active_connections
current_connections
max_connections
p95_wait_ms
timeout_count
```

## 8.2 Grow conditions

Example policy:

```text
IF
    queue_depth > 0
    AND utilization > 80%
    AND p95_wait_ms > target
THEN
    increase target pool size
```

Increase gradually.

Example:

```text
+2
+4
+8
```

Avoid uncontrolled jumps.

## 8.3 Shrink conditions

Only shrink when:

```text
queue_depth == 0
AND utilization < 30%
FOR sustained interval
```

Shrink slowly:

```text
-1
-2
```

Never shrink below:

```text
min_size
```

## 8.4 Add hysteresis

Avoid oscillation:

```text
grow threshold: 80%
shrink threshold: 30%
```

Use a cooldown:

```text
scale-up cooldown
scale-down cooldown
```

## 8.5 Add hard limits

Configurable:

```text
min_size
target_size
max_size
max_waiters
max_wait_time
scale_step
scale_up_cooldown
scale_down_cooldown
```

---

# 9. Phase 6 — Separate Thread/Executor Resources

## Goal

Prevent unrelated blocking work from competing in a single thread pool.

Current architecture should not depend on a single oversized executor for all blocking work.

Create explicit executors:

```text
DatabaseExecutor
ServiceDatabaseExecutor
BackgroundExecutor
```

Optionally:

```text
CachePersistenceExecutor
```

## Suggested responsibilities

### DatabaseExecutor

Runs:

```text
DuckDB queries
DuckDB fetches
database connection operations
```

Size approximately around the database concurrency envelope.

### ServiceDatabaseExecutor

Runs:

```text
SQLite cache metadata
API key operations
user operations
audit operations
```

Keep this smaller.

### BackgroundExecutor

Runs:

```text
cache persistence
cleanup
statistics flush
maintenance
```

## Important

Do not blindly increase the number of threads.

Too many threads can produce:

```text
context switching
CPU contention
memory overhead
scheduler overhead
```

Use measurements.

## Add executor metrics

```text
executor_name
max_workers
active_workers
queue_depth
wait_time_p50
wait_time_p95
task_duration_p95
rejected_tasks
```

The performance trace should include:

```text
DB_EXECUTOR_WAIT
SERVICE_DB_EXECUTOR_WAIT
BACKGROUND_EXECUTOR_WAIT
```

---

# 10. Phase 7 — Redesign Query Cache as L1 + L2

## Goal

Make cache hits fast enough that repeated concurrent requests do not touch SQLite or DuckDB.

## Target architecture

```text
Request
    |
    v
L1 in-memory cache
    |
    +-- HIT --> result
    |
    v
L2 persistent cache
    |
    +-- HIT --> populate L1 --> result
    |
    v
Database
```

## 10.1 L1 cache

Implement:

```text
TTL
LRU/max entries
max total bytes
optional max item size
thread-safe access
```

A possible interface:

```python
get(key)
set(key, value, ttl)
delete(key)
clear()
stats()
```

## 10.2 Cache result representation

Prefer storing an in-memory object that avoids repeated JSON serialization/deserialization where safe.

If results are mutable, use a safe immutable representation or copy policy.

## 10.3 Cache key correctness

The key should account for all result-affecting dimensions:

```text
normalized SQL
parameters
tenant
authorization scope
user scope, when relevant
database/schema version, when relevant
```

Do not include `user_id` blindly if the result is globally safe to share; unnecessary user-specific keys reduce cache hit rate.

## 10.4 Normalize SQL

Equivalent queries should ideally map to the same key where semantics are guaranteed equivalent.

At minimum:

```text
trim whitespace
normalize harmless casing
normalize parameter representation
```

Do not perform unsafe SQL rewriting.

## 10.5 Remove synchronous hit-path updates

Current cache reads perform an update similar to:

```sql
UPDATE query_cache
SET last_accessed_at = ?,
    access_count = access_count + 1
...
```

Remove this from the critical path.

Instead:

```text
L1 hit
    |
    +--> increment in-memory counter
```

Periodically flush aggregate statistics to L2.

## 10.6 Separate cache metadata from cache result retrieval

Avoid expensive operations on every request.

Hot path:

```text
lookup key
validate TTL
return result
```

Cold/background path:

```text
access statistics
last_accessed
maintenance
expired-entry cleanup
```

---

# 11. Phase 8 — Add Single-Flight / Request Coalescing

## Goal

Prevent a cache stampede.

For:

```text
500 concurrent requests
same cache key
```

the desired flow is:

```text
Request 1
    |
    +--> cache miss
    +--> create in-flight Future
    +--> execute DB query
    +--> populate cache
    +--> resolve Future

Requests 2..500
    |
    +--> cache miss
    +--> find existing in-flight Future
    +--> await same Future
```

Result:

```text
500 HTTP requests
1 DB query
499 awaiters
```

## 11.1 Single-flight registry

Create a component such as:

```text
SingleFlightCoordinator
```

Responsibilities:

```text
get_or_create(key)
release(key)
handle_success
handle_failure
handle_timeout
```

## 11.2 Failure handling

If owner request fails:

```text
all waiters receive failure
```

but:

```text
in-flight entry must be removed
```

Do not leave a permanently failed Future.

## 11.3 Cancellation handling

If a waiter cancels:

```text
remove waiter
```

The owner should generally continue unless the entire operation is cancelled by policy.

## 11.4 Do not cache errors by default

A transient DB error should not be stored as a successful cache result.

## 11.5 Limit single-flight scope

Use single-flight only for cacheable/read operations.

Never coalesce unsafe writes.

---

# 12. Phase 9 — Refactor the Query Endpoint Hot Path

## Goal

Make the endpoint easy to reason about and measurable.

The current route performs:

```text
build request context
generate cache key
cache lookup
DB execution
cache storage
response creation
```

Refactor into explicit application services.

Recommended components:

```text
QueryService
    |
    +-- QueryPolicy
    +-- QueryCacheCoordinator
    +-- SingleFlightCoordinator
    +-- DatabaseExecutor
    +-- ResultSerializer
```

## Target flow

```python
async def execute_query(request, query, context):
    validate_query(query, context)

    key = cache.make_key(query, context)

    cached = await cache.get(key)
    if cached:
        return cached

    async with single_flight(key) as flight:
        if not flight.is_owner:
            return await flight.result()

        cached = await cache.get(key)
        if cached:
            return cached

        result = await database.execute(query)

        await cache.set_l1(key, result)

        background.enqueue(
            persist_l2_cache,
            key,
            result,
        )

        flight.resolve(result)
        return result
```

The exact implementation can differ, but the responsibility boundaries should remain clear.

## Add query classification

Classify:

```text
SELECT
INSERT
UPDATE
DELETE
DDL
```

If the endpoint is intended to be a read-only query API, enforce it.

If writes are intentional:

```text
read-only scope
read-write scope
admin scope
```

Do not let unrestricted SQL execution accidentally bypass security expectations.

---

# 13. Phase 10 — Make Cache Persistence Fully Out-of-Band

## Goal

Do not delay the response waiting for L2 cache persistence.

Request path:

```text
DB result
    |
    +--> L1 cache immediately
    |
    +--> response immediately
    |
    +--> queue L2 persistence
```

Background worker:

```text
queue
    |
    v
SQLite write
```

## Add bounded queue

The background persistence queue must be bounded.

If the queue is full:

```text
drop non-critical cache write
```

or use a backpressure policy.

Do not allow unlimited memory growth.

## Add persistence metrics

```text
cache_persist_queue_depth
cache_persist_enqueued
cache_persist_completed
cache_persist_failed
cache_persist_dropped
cache_persist_latency
```

## Startup/shutdown

At shutdown:

```text
stop accepting new background jobs
drain queue within timeout
close service DB
close executors
close main DB pool
```

Do not hang shutdown indefinitely.

---

# 14. Phase 11 — Fix Performance Telemetry Persistence

## Goal

Make performance collection itself invisible to latency.

The performance architecture should remain:

```text
request profiler
    |
    v
in-memory registry
    |
    +--> periodic exporter
```

Avoid:

```text
request
    |
    +--> SQLite INSERT trace
    +--> JSON serialization
    +--> filesystem write
```

## Export strategy

Choose one or more:

```text
periodic snapshot
background export
bounded event queue
on-demand debug endpoint
```

## Sampling strategy

Use:

```text
100% sampling
```

only for controlled diagnostics.

Production:

```text
1–10%
```

or adaptive sampling.

Always keep:

```text
error requests
slow requests
timeout requests
```

high priority for sampling.

---

# 15. Phase 12 — Reliability and Concurrency Safety

## Required tests

### Pool tests

Test:

```text
100 concurrent acquire/release
500 concurrent acquire/release
connection creation races
pool exhaustion
wait timeout
cancelled waiter
broken connection
connection close
connection replacement
shutdown during active requests
```

### Cache tests

Test:

```text
L1 hit
L1 miss
L2 hit
L2 miss
TTL expiration
LRU eviction
max-size eviction
cache invalidation
corrupt cache entry
cache persistence failure
```

### Single-flight tests

Test:

```text
500 identical requests -> 1 DB execution
owner success
owner failure
owner timeout
waiter cancellation
simultaneous expiration
different keys -> independent execution
```

### Executor tests

Test:

```text
DB workload does not block service DB workload
background cache writes do not block API requests
executor shutdown
queue saturation
```

---

# 16. Phase 13 — Security and Correctness Review

## 16.1 Query execution policy

The current query route allows arbitrary SQL execution.

Decide explicitly whether the endpoint is:

```text
read-only query API
```

or:

```text
full DB console
```

If read-only:

```text
allow SELECT/WITH only
```

and reject:

```text
INSERT
UPDATE
DELETE
DROP
ALTER
CREATE
```

If writes are required:

```text
admin/elevated scope
```

should be required.

## 16.2 Cache isolation

Never return a cached result to a caller who is not authorized to see it.

Cache key design must respect:

```text
tenant
user
role
authorization scope
row-level security context
```

## 16.3 Cache invalidation

For writes:

```text
write operation
    |
    +--> determine affected cache keys
    |
    +--> invalidate L1
    |
    +--> invalidate L2
```

Avoid stale data after mutations.

## 16.4 Query result size limits

Add configurable limits:

```text
max result rows
max result bytes
max query duration
max concurrent expensive queries
```

This prevents one query from exhausting the whole pool.

---

# 17. Phase 14 — Add Concurrency Controls for Expensive Queries

Not all queries should have unlimited concurrency.

Introduce a query classification:

```text
FAST
NORMAL
EXPENSIVE
```

Example:

```text
FAST:
    SELECT 1
    small metadata queries

NORMAL:
    normal API data queries

EXPENSIVE:
    large aggregation
    wide scans
    large result sets
```

Use separate semaphores:

```text
fast_query_limit
normal_query_limit
expensive_query_limit
```

This prevents a few expensive queries from blocking all simple requests.

---

# 18. Phase 15 — Benchmark After Every Major Change

Never combine all architectural changes into one unmeasured rewrite.

Use this sequence:

## Benchmark A — Baseline

```text
pool 10
cache current
single-flight OFF
executor current
```

## Benchmark B — Pool metrics only

Verify instrumentation overhead.

## Benchmark C — Pool size tuning

Test:

```text
10
20
40
80
120
```

## Benchmark D — Adaptive controller

Compare:

```text
fixed pool
vs
adaptive pool
```

## Benchmark E — L1 cache

Test:

```text
cold cache
warm cache
```

## Benchmark F — Single-flight

Test:

```text
500 identical requests
```

Expected:

```text
DB executions ~1
```

## Benchmark G — Executor separation

Compare:

```text
single executor
vs
dedicated executors
```

## Benchmark H — Full architecture

Run:

```text
1
10
20
50
100
250
500
```

---

# 19. Required Benchmark Matrix

Every major release should generate a table like:

| Concurrency | Pool | Cache | Single-flight | Throughput | p50 | p95 | p99 | Pool Wait p95 | SQL p95 | Errors |
|---:|---:|---|---|---:|---:|---:|---:|---:|---:|---:|
| 1 | 10 | cold | off | | | | | | | |
| 10 | 10 | cold | off | | | | | | | |
| 50 | 10 | cold | off | | | | | | | |
| 100 | 10 | cold | off | | | | | | | |
| 250 | 10 | cold | off | | | | | | | |
| 500 | 10 | cold | off | | | | | | | |
| 500 | 40 | cold | off | | | | | | | |
| 500 | 80 | cold | off | | | | | | | |
| 500 | 40 | warm | off | | | | | | | |
| 500 | 40 | warm | on | | | | | | | |

This table becomes the source of truth for optimization decisions.

---

# 20. Expected Performance Progression

The goal is to move through these stages:

## Current

```text
500 requests
    |
    +--> 10 DB connections
    |
    +--> queue buildup
    |
    +--> p95 ~8.95 seconds
```

## After pool tuning

```text
500 requests
    |
    +--> optimized DB concurrency
    |
    +--> much smaller pool wait
```

## After L1 cache

```text
500 repeated requests
    |
    +--> most served from memory
```

## After single-flight

```text
500 repeated requests
    |
    +--> 1 DB query
    +--> 499 await shared result
```

## Desired result

```text
500 HTTP requests
    |
    +--> 1 database operation
    +--> minimal pool contention
    +--> minimal SQLite activity
    +--> low serialization overhead
```

---

# 21. Recommended Repository Refactor

Suggested target organization:

```text
core/
├── app/
│   ├── api/
│   │   ├── routes.py
│   │   └── dependencies.py
│   └── lifespan.py
│
├── db/
│   ├── pool/
│   │   ├── async_pool.py
│   │   ├── sync_pool.py
│   │   ├── adaptive.py
│   │   ├── metrics.py
│   │   └── policies.py
│   │
│   ├── execution/
│   │   ├── executor.py
│   │   ├── query_runner.py
│   │   └── query_limits.py
│   │
│   └── settings.py
│
├── cache/
│   ├── l1.py
│   ├── l2.py
│   ├── coordinator.py
│   ├── singleflight.py
│   ├── keys.py
│   └── invalidation.py
│
├── concurrency/
│   ├── executors.py
│   ├── semaphores.py
│   └── queues.py
│
├── performance/
│   ├── request_profiler.py
│   ├── registry.py
│   ├── histogram.py
│   ├── stages.py
│   └── exporters/
│
└── services/
    ├── query_service.py
    ├── cache_service.py
    └── auth_service.py
```

This is a target architecture, not a requirement to move every file immediately. Refactor incrementally.

---

# 22. Recommended Implementation Sequence

## Sprint / Stage 1 — Measurement

Implement:

```text
1. pool queue depth
2. pool wait histograms
3. executor wait metrics
4. cache L1/L2 metrics
5. single-flight metrics
6. full request stage timing
```

Do not change behavior yet.

---

## Sprint / Stage 2 — Pool Correctness

Implement:

```text
1. explicit connection states
2. safe acquire/release
3. no connection creation under lock
4. cancellation safety
5. connection health checks
6. broken connection replacement
7. timeout categories
```

Run race-condition tests.

---

## Sprint / Stage 3 — Pool Tuning

Implement:

```text
1. configurable min/max
2. fixed-size benchmark runs
3. determine optimal target
4. record benchmark matrix
```

Use actual data to choose defaults.

---

## Sprint / Stage 4 — Adaptive Pool

Implement:

```text
1. queue-aware scaling
2. utilization-aware scaling
3. p95-wait-based scaling
4. hysteresis
5. cooldown
6. hard min/max limits
```

Run fixed vs adaptive comparison.

---

## Sprint / Stage 5 — Executor Isolation

Implement:

```text
1. DB executor
2. service DB executor
3. background executor
4. executor metrics
5. bounded queues
```

Remove dependence on an oversized global executor.

---

## Sprint / Stage 6 — Cache L1

Implement:

```text
1. in-memory TTL/LRU
2. bounded size
3. cache metrics
4. correct cache keys
5. authorization-aware cache scope
```

Verify warm-cache latency.

---

## Sprint / Stage 7 — Single-Flight

Implement:

```text
1. per-key in-flight registry
2. owner/waiter model
3. failure cleanup
4. cancellation handling
5. timeout handling
```

Verify:

```text
500 identical queries
=> approximately 1 DB execution
```

---

## Sprint / Stage 8 — Query Service Refactor

Move route logic into:

```text
QueryService
QueryCacheCoordinator
SingleFlightCoordinator
DatabaseExecutor
```

Keep routes thin.

---

## Sprint / Stage 9 — Background Persistence

Move:

```text
L2 cache writes
access statistics
maintenance
cleanup
```

off the synchronous request path.

---

## Sprint / Stage 10 — Security and Limits

Implement:

```text
query policy
read/write authorization
query timeout
result-size limits
expensive-query concurrency limits
cache isolation
invalidation rules
```

---

## Sprint / Stage 11 — Full Load Validation

Run:

```text
1
10
20
50
100
250
500
```

with:

```text
cold cache
warm cache
identical queries
unique queries
mixed queries
```

Compare against the baseline report.

---

# 23. Definition of Done

The refactor is considered successful when all of the following are true.

## Performance

- [ ] p95 latency is substantially below the current ~8.95 s baseline at 500 concurrent requests.
- [ ] Pool wait is proven to be bounded and measurable.
- [ ] SQL execution time is separated from pool queue time.
- [ ] Warm-cache requests do not require DuckDB access.
- [ ] Identical concurrent queries are coalesced.
- [ ] Throughput improves without uncontrolled CPU/thread growth.

## Pool

- [ ] No pool lock is held during slow connection creation.
- [ ] No connection leaks.
- [ ] No closed connection is reused.
- [ ] Cancellation is safe.
- [ ] Pool timeouts are categorized.
- [ ] Adaptive sizing uses queue pressure and wait latency.
- [ ] Pool size is benchmark-derived.

## Cache

- [ ] L1 cache implemented.
- [ ] L2 persistent cache retained where required.
- [ ] Cache-hit path does not synchronously update SQLite.
- [ ] Cache keys respect authorization boundaries.
- [ ] TTL and invalidation are correct.
- [ ] Cache stampede is prevented.

## Executors

- [ ] Database work has a dedicated executor.
- [ ] Service DB work has a dedicated executor.
- [ ] Background persistence has a bounded queue.
- [ ] Executor queue latency is measurable.

## Performance subsystem

- [ ] Profiling overhead is low.
- [ ] No synchronous telemetry I/O on request path.
- [ ] p50/p95/p99 stage metrics available.
- [ ] Pool wait is visible.
- [ ] Executor wait is visible.
- [ ] Cache and single-flight behavior are visible.

## Reliability

- [ ] Concurrency tests pass.
- [ ] Race-condition tests pass.
- [ ] Cancellation tests pass.
- [ ] Shutdown tests pass.
- [ ] Cache failure does not fail successful API queries.
- [ ] Background queue saturation does not crash the API.

## Security

- [ ] Query execution policy is explicit.
- [ ] Write-capable SQL is appropriately protected.
- [ ] Cache isolation is verified.
- [ ] Query/result resource limits are enforced.

---

# 24. Final Priority Order

Implement in this order:

```text
P0-1  Instrument pool wait and executor wait
P0-2  Refactor pool locking and lifecycle
P0-3  Benchmark pool sizes
P0-4  Replace hit-ratio adaptive scaling with queue-aware scaling
P0-5  Add L1 memory cache
P0-6  Remove SQLite writes from cache-hit hot path
P0-7  Add single-flight request coalescing
P0-8  Separate DB/service/background executors
P0-9  Refactor query endpoint into QueryService
P0-10 Add query/result/concurrency limits
P0-11 Add cache invalidation and authorization-safe cache keys
P0-12 Run full 1–500 concurrency benchmark matrix

P1-1  Improve connection health scoring
P1-2  Add expensive-query classification
P1-3  Add bounded background persistence queues
P1-4  Add adaptive performance sampling
P1-5  Add production alerts and operational dashboards
```

---

# 25. Most Important Technical Principle

Do not solve this as:

```text
500 requests
    ->
increase pool to 500
```

Solve it as:

```text
500 HTTP requests
    |
    +--> fast auth/cache path
    |
    +--> duplicate requests coalesced
    |
    +--> only required work reaches DB
    |
    +--> DB concurrency bounded to measured capacity
    |
    +--> pool grows based on real queue pressure
    |
    +--> background persistence is isolated
    |
    +--> performance telemetry proves every stage
```

The desired architecture is therefore:

```text
HIGH HTTP CONCURRENCY
        |
        v
LOW UNNECESSARY WORK
        |
        v
BOUNDED DB CONCURRENCY
        |
        v
MEASURED POOL WAIT
        |
        v
FAST CACHE + SINGLE-FLIGHT
        |
        v
LOW TAIL LATENCY
```

This roadmap should be executed incrementally. Each phase must have a benchmark before and after it, and no later phase should be used to hide regressions introduced by an earlier phase.