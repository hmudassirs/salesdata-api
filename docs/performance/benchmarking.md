# Benchmarking (Phase 12)

`benchmarks/` holds standalone scripts that exercise the real
subsystems end to end — a real `SyncConnectionPool`, a real temp-file
SQLite `ServiceDatabase`, a real FastAPI app served over
`httpx.ASGITransport` — the same way `load_test.py` already exercises
the live API, rather than mocking anything. They live outside
`core/performance` entirely: `docs/PerformancePlan.md`'s package
boundary ("the core never depends on [middleware/collectors/exporters/
dashboard]") is about layering *within* `core/performance`, and does
not cover these scripts.

Every script shares `benchmarks/_common.py`:

- `BenchmarkResult` — one named run's iteration count, error count,
  and per-call durations, with `throughput_per_second`,
  `error_rate`, and `percentile(pct)` computed from those.
- `run_benchmark(name, iterations, call)` — runs `call()` `iterations`
  times, timing each call and tallying (not raising on) exceptions.
- `render_report(results)` — an aligned, human-readable summary.
- `compare(baseline, candidate)` — a before/after diff: throughput and
  p50/p90/p95/p99 latency deltas, plus the error-count change.
- `stage_breakdown(registry)` / `render_stage_breakdown(...)` — walks
  every retained `RequestProfile`'s trace tree (see `README.md`'s
  pipeline section) and summarizes mean/p95 duration per trace-node
  name, regardless of which request or how deep in the tree it
  occurred — this is what turns a raw latency number into "where did
  the time go" (`pool_acquire` vs `sql_execute` vs ...), matching
  `docs/PerformancePlan.md`'s objective of breaking a benchmark's
  latency down by stage before any optimisation claim is made.

## The four scripts

Run any of them with `python -m benchmarks.<name> [--iterations N]
[--json-out path.json]`.

### `benchmark_timer.py`

Pure in-process overhead of the profiling primitives themselves — no
I/O. Three named runs of the same simulated work (three nested
`stage()` calls, a counter, a gauge):

- `no-profiler` — the work runs with no profiler at all (a wall-clock
  floor for the simulated work itself).
- `null-profiler` — routed through `NullRequestProfiler`, the disabled
  path.
- `real-profiler` — routed through a real `RequestProfiler`.

`null-profiler` vs `no-profiler` is the fixed cost of the disabled
interface; `real-profiler` vs `null-profiler` is what enabling
profiling actually costs per request.

### `benchmark_pool.py`

`core.db.pool.SyncConnectionPool.acquire()`/`release()` throughput:
`raw-pool` (direct) vs `instrumented-pool` (wrapped in
`SyncPoolTimingAdapter`, unsampled — no profiler bound) vs
`instrumented-pool-profiled` (wrapped, with a real profiler bound for
every call).

### `benchmark_sql.py`

`core.storage.service_db.ServiceDatabase.fetch_one()` against a real
temp-file SQLite database seeded with 200 rows: `raw-sql` vs
`instrumented-sql` (wrapped in `InstrumentedServiceDatabase`, unsampled)
vs `instrumented-sql-profiled` (wrapped, sampled).

### `benchmark_api.py`

The most complete of the four: builds one minimal FastAPI app with a
`/fast` (immediate-return) and a `/query` (SQL read, dispatched through
`asyncio.to_thread` the way the real query route keeps synchronous DB
work off the event loop) route, drives it under concurrent load via
`httpx.AsyncClient`/`ASGITransport` (`--concurrency`, default 10), and
compares:

- `baseline` — `install_performance_middleware` with
  `PerformanceConfig(enabled=False)`.
- `candidate` — the same app, `PerformanceConfig(enabled=True,
  sample_rate_percent=100)`.

...then prints `compare(baseline, candidate)` and, since only
`candidate` retains request history, its `stage_breakdown`. This is the
one script whose output most directly answers
`docs/PerformancePlan.md`'s objective: instead of one aggregate
"~500 concurrent requests, ~6.7s average latency" number, the
trace-stage breakdown shows how that latency splits across
`pool_acquire`, `sql_execute`, response serialization, etc. — see
`README.md`'s pipeline section.

## Reading a report

```text
baseline (baseline) vs candidate (candidate):
  throughput : 2157.7 -> 1914.2 ops/sec (-11.3%)
  p50 latency : 3.985ms -> 4.850ms (+21.7%)
  ...
  errors     : 0/200 -> 0/200

candidate trace-stage breakdown:
  request                  count=200    mean=2.966ms p95=4.983ms
  dispatch                 count=200    mean=2.945ms p95=4.970ms
  service_db_fetch_one     count=100    mean=0.051ms p95=0.115ms
```

Small `--iterations`/`--concurrency` runs are noisy (system load,
container scheduling jitter) — the sample run above shows profiling
overhead within a few percent of baseline, well below run-to-run
variance at these sample sizes. Use a larger `--iterations` for a
signal worth acting on, and prefer the trace-stage breakdown (which
isolates the request's own work) over the top-line ops/sec number
(which also captures the benchmark harness's own scheduling overhead)
when deciding where to optimise — see Phase 14's before/after report for
a worked example of exactly this.

## Testing

The scripts' *value* is in exercising real subsystems end to end, so
they are verified by running them (see each script's own output above)
rather than by unit test. What is unit-tested
(`benchmarks/tests/test_common.py`) is the reusable, pure logic every
script depends on: percentile/throughput computation, `compare()`'s
diffing, and `stage_breakdown()`'s trace-tree walk.
