# Performance instrumentation

`core.performance` is a framework-independent tracing/metrics engine,
built per `docs/PerformancePlan.md` before any optimisation work is
allowed. This directory documents how to operate it. For the design
rationale and phase-by-phase plan, see `docs/PerformancePlan.md`; this
is the operational counterpart — what to import, how to wire it up,
and what each piece does.

## The pipeline

```text
Trace/Event -> Collector -> Registry -> Aggregator -> Exporter
```

- **Trace/Event** (`trace.py`, `timer.py`, `request_profiler.py`): a
  `RequestProfiler` owns one request-local `Trace` — a tree of nested
  `TraceNode`s, one per `stage()` call — plus a flat list of counter/
  gauge/event `MetricPoint`s. Nothing here is shared across requests or
  protected by a lock; each request's data lives only on its own
  profiler until `complete()` hands back an immutable `RequestProfile`.
- **Collector** (`registry.Collector`, `collectors/`): two different
  things share this name deliberately kept apart —
  - a `registry.Collector` observes one just-completed `RequestProfile`
    synchronously (must be cheap; no I/O);
  - a `collectors.ResourceCollector` (Phase 9) samples *process-wide*
    state (memory, GC, threads, ...) on demand, independent of any one
    request.
- **Registry** (`registry.py`): the one stateful, shared object in the
  subsystem. `PerformanceRegistry.record_completed_request()` is the
  single method called from the request path — it folds a profile's
  metric points into the aggregator and appends to a bounded history
  ring buffer, with no I/O.
- **Aggregator** (`aggregator.py`, `histogram.py`): turns raw
  `MetricPoint`s into running `Counter`/`Gauge` state and streaming
  `StreamingHistogram` percentile estimates (P² algorithm — O(1) memory
  per histogram, not exact but converges to accurate quantiles).
- **Exporter** (`registry.Exporter`, `exporters/`): reads current
  registry state and publishes it externally (JSON/CSV/console file or
  stream, Prometheus text, OpenTelemetry instruments) — always out of
  band, never on the request path.

## Package map

```text
core/performance/
  config.py constants.py exceptions.py enums.py types.py clock.py   # foundation
  trace.py timer.py context.py request_profiler.py                  # trace/event
  metric.py histogram.py registry.py aggregator.py                  # aggregation
  middleware/    # FastAPI request lifecycle wiring
  adapters/      # timing wrappers around existing subsystems (pool, SQL, auth, ...)
  collectors/    # optional process-wide resource sampling
  exporters/     # optional JSON/CSV/console/Prometheus/OTel output
  dashboard/     # optional read-only /debug/* FastAPI routes
  tests/
benchmarks/      # standalone scripts exercising the real subsystems end to end
```

Foundation and aggregation point inward only. `middleware/`,
`adapters/`, `collectors/`, `exporters/`, and `dashboard/` may depend on
the core; the core never imports any of them. `benchmarks/` sits
outside `core/performance` entirely — see `benchmarking.md`.

## Quick start: wiring a FastAPI app

```python
from fastapi import FastAPI

from core.performance.config import PerformanceConfig
from core.performance.middleware.fastapi import install_performance_middleware
from core.performance.dashboard import install_performance_dashboard
from core.performance.exporters import PrometheusExporter
from core.performance.registry import get_default_registry

app = FastAPI()

# 1. Profile sampled requests. Defaults to PerformanceConfig.from_env()
#    and the process-wide default registry if omitted.
install_performance_middleware(app, config=PerformanceConfig.from_env())

# 2. Optional: expose the read-only debug dashboard.
install_performance_dashboard(app)  # GET /debug/performance, /debug/request/{id}

# 3. Optional: register an exporter and run it out of band (e.g. from a
#    periodic task or the dashboard), never inline with a request.
get_default_registry().register_exporter(PrometheusExporter(path="/tmp/metrics.prom"))
```

With no configuration at all (`PerformanceConfig()`, `enabled=False`),
every call in this subsystem is a no-op: `install_performance_middleware`
binds a `NullRequestProfiler` for the request and nothing is timed,
allocated, or recorded. See `configuration.md` for every field and its
environment variable.

## Live in this app

`core.performance` is now wired into the real request path, not just
available for opt-in use:

- **Connection pool** — `core.db.session.DatabaseSession` wraps its
  async/sync pools in `AsyncPoolTimingAdapter`/`SyncPoolTimingAdapter`;
  every `get_async_session()`/`get_sync_session()` acquire/release is
  timed and publishes `pool_active_connections`/`pool_idle_connections`
  gauges.
- **SQL execution path** — every pooled connection's adapter (DuckDB or
  SQLite, whichever is configured) is wrapped in the new
  `adapters.sql_adapter.InstrumentedSQLAdapter`; `execute`/`fetch_one`/
  `fetch_all` are timed under `SQL_EXECUTE`/`SQL_FETCH`.
- **Transaction manager** — `DatabaseSession.get_async_transaction()`/
  `get_sync_transaction()` expose `adapters.transactions`'s instrumented
  transaction context managers. **Not** force-adopted into `/api/query`
  itself: that route calls `fetch_all` for every statement today
  (`core.db.transactions.transaction`/`async_transaction` aren't used
  anywhere in this app), and wrapping writes in an explicit transaction
  would be a behavior change beyond what instrumentation should make
  silently. The instrumented version is available for any write path
  that wants it.
- **Request throughput/latency + CPU/memory** —
  `core.app.api.app.create_app` installs
  `install_performance_middleware` (outermost, after auth, so it covers
  rejected/unauthenticated requests too) and `core.app.lifespan
  .PerformanceStep` optionally starts a `CollectorScheduler` for CPU/
  memory/GC/thread collectors — both disabled by default, per
  `PerformanceConfig`'s fail-safe philosophy; see `configuration.md`
  for the env vars to turn them on.
- **Live dashboard** — `create_app` also installs
  `install_performance_dashboard(app, dependencies=[Depends(require_admin_user)])`.
  `/debug/*` was added to `core.auth.middleware`'s protected-path check
  (it doesn't share the `/api` prefix that check was originally scoped
  to) so the admin gate actually has a caller identity to check against.

See `collectors-exporters-dashboard.md` for the live dashboard page
itself, and `adapters.md` for the pool/SQL adapters.

## Further reading

- `configuration.md` — every `PerformanceConfig` field, its
  `PERF_*` environment variable (where one exists), and sampling
  semantics.
- `adapters.md` — the Phase 5–8 timing wrappers around the pool,
  transactions, auth, DuckDB, and SQLite/cache subsystems.
- `collectors-exporters-dashboard.md` — Phases 9–11: optional resource
  collectors, output exporters, and the read-only debug dashboard.
- `benchmarking.md` — the `benchmarks/` scripts, what each one
  measures, and how to read a before/after comparison.
- `phase-14-optimization-report.md` — the one measured optimisation
  Phase 14 authorizes: profile, evidenced bottleneck, fix, re-benchmark.
