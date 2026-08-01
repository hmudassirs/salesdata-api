# Collectors, exporters, and the debug dashboard (Phases 9–11)

All three packages are optional: nothing in `core.performance`'s
foundation, trace/event, or aggregation layers imports any of them, and
none are wired in automatically. Each is opt-in wiring on top of the
same `PerformanceRegistry`.

## Collectors (`collectors/`)

Two different things share the name "collector" in this subsystem —
worth keeping straight:

- `registry.Collector` (Phase 2): observes one just-completed
  `RequestProfile`, called synchronously by
  `PerformanceRegistry.record_completed_request`. Must be cheap.
- `collectors.ResourceCollector` (Phase 9, this section): samples
  *process-wide* resource state on demand — has no owning request.

Six stdlib-only `ResourceCollector`s, each returning
`list[MetricPoint]` (gauges) and degrading to `[]` rather than raising
if the platform facility it reads is unavailable:

| Collector | Reads |
|---|---|
| `MemoryCollector` | `resource.getrusage().ru_maxrss` (peak RSS), `/proc/self/status`'s `VmRSS` (current RSS, Linux-only), `tracemalloc` if already running |
| `GCCollector` | `gc.get_count()` (current per-generation object counts), `gc.get_stats()` (cumulative collections/collected/uncollectable) |
| `ThreadCollector` | `threading.enumerate()` — total/daemon/non-daemon counts |
| `AsyncioCollector` | `asyncio.all_tasks()` — total/pending counts; `[]` if no event loop is running |
| `CPUCollector` | `os.times()` — cumulative user/system CPU **seconds**, a running total (not a percentage — see the module docstring for why) — plus `os.cpu_count()` |
| `ProcessCollector` | `os.getpid()`, `/proc/self/fd` (open fd count, Linux-only), `resource.getrlimit(RLIMIT_NOFILE)` (max fds) |

`build_enabled_collectors(config: PerformanceConfig)` turns the six
`collect_*` flags (see `configuration.md`) into the matching collector
list. `collect_all(collectors)` runs a list once and flattens the
result. `CollectorScheduler(collectors, registry, interval_seconds=15.0)`
is the optional background runner — `start()`/`stop()` own an asyncio
task that calls `collect_once()` on an interval, feeding
`registry.aggregator.ingest(...)` directly (bypassing
`record_completed_request`, since these samples have no owning
request). Nothing starts it automatically; wire it from application
startup/shutdown code the way `ObservabilityWriteQueue`'s background
worker is owned by the app lifespan.

```python
from core.performance.collectors import CollectorScheduler, build_enabled_collectors
from core.performance.registry import get_default_registry

registry = get_default_registry()
scheduler = CollectorScheduler(build_enabled_collectors(config), registry)
scheduler.start()   # at app startup
...
await scheduler.stop()   # at app shutdown
```

## Exporters (`exporters/`)

Every exporter implements `registry.Exporter` (`name: str` and
`export(registry) -> None`), and every one reads through the shared
`exporters.snapshot.build_snapshot(registry)` helper — the one place
that reads `registry.counters()`/`gauges()`/`histograms()` — rather
than duplicating that read per exporter.

| Exporter | Output | Notes |
|---|---|---|
| `JSONExporter` | one JSON object: `counters`/`gauges`/`histograms` | Optional `path` writes to disk; `last_output` always holds the rendered text. |
| `CSVExporter` | one flat CSV row per metric | Histograms spread their `snapshot()` fields (count/sum/mean/min/max/p50/p90/p95/p99) across extra columns instead of nesting. |
| `ConsoleExporter` | human-readable text report | Writes to a `stream` (default `sys.stdout`). |
| `PrometheusExporter` | Prometheus text exposition format | Hand-formatted, not via `prometheus_client` — keeps this from sharing (and risking a name collision with) `core.observability.prometheus_metrics`'s own `CollectorRegistry`, a separate existing metrics surface. Histograms render as standard `_bucket`/`_sum`/`_count` lines from `StreamingHistogram`'s cumulative bucket counts. |
| `OTelExporter` | real `opentelemetry-api` instruments | Uses whatever `MeterProvider` the app configured (see `core.observability.otel`), or a working no-op meter if none was set up. Counters use `Counter.add()` with delta-tracking (an internal `name+tags -> last exported total` cache) so repeated exports of the same cumulative counter don't double-count; gauges (and each histogram's per-quantile stats, tagged `stat=`) use the synchronous `Gauge.set()`. |

None are registered automatically:

```python
from core.performance.exporters import JSONExporter
from core.performance.registry import get_default_registry

registry = get_default_registry()
registry.register_exporter(JSONExporter(path="/tmp/perf-metrics.json"))
registry.export_all()          # or registry.export("json") for just one
```

## Debug dashboard (`dashboard/`)

Read-only. `install_performance_dashboard(app, registry=None,
prefix="/debug", dependencies=None, refresh_interval_seconds=2.0)`
registers three routes on a FastAPI app:

- `GET {prefix}/` — a lightweight, self-contained, auto-refreshing HTML
  page (`dashboard/live.py`'s `render_dashboard_html`, no build step,
  no external JS/CSS): throughput (`total_requests_recorded` delta over
  wall-clock time between polls), request latency percentiles (the
  `request`/`dispatch` histogram), pool contention
  (`pool_active_connections`/`pool_idle_connections` gauges), every
  other trace-stage's timing (SQL, transactions, auth, ...), CPU/memory
  gauges if the Phase 9 collectors are enabled, and a recent-requests
  table linking to `{prefix}/request/{id}`. It's plain `fetch()`
  polling against `{prefix}/performance` on a timer — refresh the JSON
  and this page reflects it, nothing server-rendered per poll.
- `GET {prefix}/performance` — the JSON this page (or anything else)
  polls: current counters/gauges/histograms plus the most-recently-
  completed requests (bounded by `recent_limit`, default 20, most
  recent first) and `total_requests_recorded` (a monotonic total,
  unaffected by history eviction — see `PerformanceRegistry
  .total_requests_recorded`'s docstring for why this differs from
  `request_count`). Built by the framework-independent
  `dashboard.summary.build_performance_summary(registry, recent_limit=...)`,
  directly unit-testable without a FastAPI test client.
- `GET {prefix}/request/{request_id}` — one retained request's full
  trace tree and metric points, via `RequestProfile.to_dict()`. `404`
  if the id was never retained or has been evicted from the bounded
  history (see `max_request_history` in `configuration.md`).

`registry` defaults to the same process-wide default registry
`install_performance_middleware` uses, so wiring both with no arguments
is enough for the dashboard to reflect what the middleware recorded.
The dashboard knows nothing about auth — pass `dependencies` (an
`APIRouter`-style list of `Depends(...)`) if the routes should require
it in a given deployment:

```python
from fastapi import Depends
from core.auth.dependencies import require_admin_user
from core.performance.dashboard import install_performance_dashboard

install_performance_dashboard(app, dependencies=[Depends(require_admin_user)])
```

This is exactly what `core.app.api.app.create_app` does for the real
app — see `README.md`'s "Live in this app" section for why `/debug`
also had to be added to `core.auth.middleware`'s protected-path check
for that gate to work at all.
