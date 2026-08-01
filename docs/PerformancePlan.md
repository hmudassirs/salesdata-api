# Performance Instrumentation Plan

## Objective

Add a production-grade, framework-independent tracing engine under
`core.performance` before attempting performance optimisation. The engine
models sampled requests as nested trace trees, emits typed events, aggregates
metrics outside the request path, and supports optional exporters and FastAPI
middleware.

The benchmark showing roughly 500 concurrent `SELECT 1` requests, about
6.7-second average latency, and about 41 requests/second is a baseline only.
No root-cause claim or optimisation should be made until a profiler report
breaks the latency into authentication, dependency, cache, pool, lock, SQL,
serialization, and response time.

## Non-negotiable design rules

- Python 3.12+, full type hints, `dataclass(slots=True)`, Ruff-clean, and mypy
  strict for new code.
- Internal time is `perf_counter_ns()` integer nanoseconds. Conversion happens
  only at exporter or presentation boundaries.
- Standard stages use `PerformanceStage`; instrumentation does not create
  ad-hoc stage strings.
- Tags distinguish dimensions such as database, route, role, and connection;
  metric names do not encode those dimensions.
- A sampled request owns its trace tree. Context propagation is request-local;
  no process-wide lock is acquired to append a node.
- The disabled path is a no-op: no timing, trace, metric, exporter, logging,
  formatting, or allocation work.
- Exporters and aggregation are not allowed to block request handling.
- Existing observability remains separate. Any Prometheus or OpenTelemetry
  bridge is an optional exporter, not a core dependency.

## Event pipeline

```text
Trace/Event -> Collector -> Registry -> Aggregator -> Exporter
```

`RequestProfiler` creates request-local `TraceNode` objects. On completion it
hands immutable event/metric data to collectors. The registry owns configured
collectors and exporters. The aggregator maintains counters, gauges,
histograms, and streaming percentile state. Exporters convert completed data
to external formats. These roles remain separate; a timer alone never
calculates an average or exports a report.

## Phased delivery

### Phase 0 — Architecture and plan

Read the project and create `Architecture.md`, `RequestFlow.md`, and this
document. No application or profiler code changes. Exit gate: the documented
seams preserve current middleware order, dependency APIs, database ownership,
and existing observability.

### Phase 1 — Foundation

Complete `config.py`, `constants.py`, `exceptions.py`, `types.py`,
`enums.py`, `clock.py`, `trace.py`, and `timer.py`. This phase has no
FastAPI, database, registry, collector, or exporter import. It supplies typed
nanosecond values, standard stages, trace-tree semantics, sampling
configuration, and tests for all public behaviour.

### Phase 2 — Metrics core

Implement `metric.py`, `histogram.py`, `registry.py`, `aggregator.py`,
and `context.py`. Add counters, gauges, histograms, streaming percentiles,
request-local storage, and thread/context-safe handoff without a global hot
path lock.

### Phase 3 — Request profiler

Implement `request_profiler.py`: nested stages, automatic finish, metadata and
tags, trace export to dict/JSON, and completion events. The profiler is still
usable in scripts, workers, and tests with no web dependency.

### Phase 4 — Middleware

Implement `middleware/fastapi.py` as the only FastAPI-dependent layer. It
creates a `RequestProfiler`, attaches it to `request.state`, records request
metadata, completes it on normal/error/cancellation paths, and pushes results
to the configured registry. It does not know database, pool, or auth details.

### Phases 5–8 — Existing subsystem adapters

Add optional adapters, preferably wrappers, around connection pools and
transactions (Phase 5), authentication and authorisation (Phase 6), DuckDB
execution/fetching (Phase 7), and SQLite service-database/cache operations
(Phase 8). Each change is narrow, keeps existing call signatures and exception
behaviour, and is accompanied by regression tests.

### Phases 9–11 — Collection, export, and dashboard

Implement optional memory, GC, thread, asyncio, CPU, and process collectors;
JSON, CSV, console, Prometheus, and future OpenTelemetry exporters; then a
read-only optional dashboard adapter exposing `/debug/performance` and
`/debug/request/{id}`. Core profiling remains independent of these adapters.

### Phase 12 — Benchmarking

Add `benchmarks/benchmark_timer.py`, `benchmark_pool.py`,
`benchmark_sql.py`, and `benchmark_api.py`. Reports compare named baseline
and candidate runs, including throughput, latency percentiles, errors, and
trace-stage breakdowns.

### Phases 13–14 — Documentation and measured optimisation

Add operational documentation and examples. Only then profile the current
benchmark, select one evidenced bottleneck, change it, run the same benchmark,
and record a before/after report. No blind optimisation.

## Phase gates

Each phase must compile, pass Ruff, pass mypy strict for new code, and pass its
tests before the next phase begins. Every public symbol has a docstring, every
new module has direct tests, and no module is added as a placeholder.

## Package boundary

```text
core/performance/
  config.py constants.py exceptions.py enums.py types.py clock.py
  trace.py timer.py context.py request_profiler.py
  metric.py histogram.py registry.py aggregator.py
  middleware/ collectors/ exporters/ dashboard/ tests/
```

Foundation and core layers point inward only. Middleware, collectors,
exporters, dashboard, and application-specific instrumentation may depend on
the core; the core never depends on them.

