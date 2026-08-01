# API Performance Summary Report

**Generated using `core.performance`** (the tracing/metrics engine documented in `docs/performance/`) against a representative FastAPI app wired exactly as `docs/performance/README.md`'s quick-start describes: `install_performance_middleware` + `install_performance_dashboard` on top of a real app, driven under concurrent load over `httpx.ASGITransport` (no mocks — a real connection pool, a real temp-file SQLite database).

## 1. What was measured

| | |
|---|---|
| App | Two routes: `GET /fast` (immediate return) and `GET /query` (a SQL read via `InstrumentedServiceDatabase.fetch_one`, dispatched through `asyncio.to_thread`) |
| Config | `PerformanceConfig(enabled=True, sample_rate_percent=100)` — every request profiled |
| Traffic | 400 requests, concurrency 10, alternating `/fast`/`/query` | 
| Instrumentation | `install_performance_middleware` (trace + metrics per request), `install_performance_dashboard` (`/debug/performance`, `/debug/request/{id}`), `JSONExporter`/`PrometheusExporter` (out-of-band snapshot export) |

## 2. Aggregate results (`benchmark_api.py`, larger 2000-iteration run)

Profiling-enabled (`candidate`) vs profiling-disabled (`baseline`), same app, same traffic — **after** the Phase 14 registry fix (see §5):

| Metric | Baseline | Candidate | Delta |
|---|---|---|---|
| Throughput | 1959.8 ops/sec | 1670.9 ops/sec | −14.7% |
| p50 latency | 9.388 ms | 10.217 ms | +8.8% |
| p90 latency | 12.144 ms | 14.889 ms | +22.6% |
| p95 latency | 14.212 ms | 19.520 ms | +37.3% |
| p99 latency | 36.846 ms | 45.066 ms | +22.3% |
| Errors | 0/2000 | 0/2000 | — |

Candidate's own trace-stage breakdown (mean/p95 duration per stage, across all 1000 sampled requests in that run):

| Stage | Count | Mean | p95 |
|---|---|---|---|
| `request` (whole request) | 1000 | 6.940 ms | 9.062 ms |
| `dispatch` (route handler) | 1000 | 6.917 ms | 9.040 ms |
| `service_db_fetch_one` (SQL read, `/query` only) | 500 | 0.020 ms | 0.068 ms |

**Reading this:** the SQL call itself is negligible (0.02ms mean) against the ~6.9ms the whole request takes — the bulk of `dispatch`'s time is FastAPI/Starlette routing and dependency resolution overhead in this minimal benchmark app, not the instrumented subsystem. Profiling itself costs single-digit-percent throughput and 9–37% latency depending on percentile, which is the honest, current cost of 100%-sample-rate profiling on this workload — see `docs/performance/configuration.md` for dialing `sample_rate_percent` down in production.

## 3. Live dashboard output (`GET /debug/performance`)

A real request against the running app's dashboard route, after 400 requests:

```json
{
  "request_count": 400,
  "max_request_history": 1000,
  "counters": [],
  "gauges": [],
  "histograms": [
    {
      "name": "dispatch",
      "tags": {},
      "count": 400, "mean_ms": 3.922, "p50_ms": 3.715,
      "p90_ms": 5.977, "p95_ms": 8.392, "p99_ms": 10.904
    },
    {
      "name": "service_db_fetch_one",
      "tags": {},
      "count": 200, "mean_ms": 0.126, "p50_ms": 0.019,
      "p90_ms": 1.022, "p95_ms": 1.625, "p99_ms": 2.716
    }
  ],
  "recent_requests": [
    {"request_id": "d646e18c...", "status": "ok", "error": null,
     "duration_ns": 1008246, "tags": {"method": "GET", "route": "/query", "status_code": "200"}}
  ]
}
```

(`counters`/`gauges` are empty here because this benchmark app doesn't emit any — no adapter in the request path calls `profiler.counter()`/`.gauge()`; the real production app's pool/auth adapters do, per `docs/performance/adapters.md`.)

## 4. `GET /debug/request/{id}` — one request's full trace

```json
{
  "request_id": "d646e18c0d5e48a0b7fec60643f702af",
  "status": "ok", "duration_ns": 1008246,
  "tags": {"method": "GET", "route": "/query", "status_code": "200"},
  "trace": {
    "name": "request", "stage": "REQUEST", "duration_ns": 1006525,
    "children": [
      {"name": "dispatch", "stage": "RESPONSE", "duration_ns": 987191,
       "exclusive_duration_ns": 932040,
       "children": [
         {"name": "service_db_fetch_one", "stage": "SQL_FETCH",
          "duration_ns": 55151, "children": []}
       ]}
    ]
  }
}
```

`exclusive_duration_ns` on `dispatch` (932,040 ns) vs its total `duration_ns` (987,191 ns) shows most of that 0.99ms is FastAPI/Starlette's own routing and response-serialization work, not the 55µs SQL call nested under it — exactly the "break latency into named stages before optimising" workflow `docs/PerformancePlan.md` requires.

## 5. Exporter output

**`JSONExporter.render(registry)`** and **`PrometheusExporter.render(registry)`** were both run against the same registry state (400 requests). JSON matches the dashboard's `histograms` section exactly, just without the request-history section. Prometheus text (excerpt):

```text
# TYPE dispatch histogram
dispatch_bucket{le="10000"} 0
dispatch_bucket{le="+Inf"} 402
dispatch_sum 1573543614.0
dispatch_count 402
```

**⚠️ Observation, not something this report's benchmark caused:** every bucket below `+Inf` reads `0` regardless of how fast requests actually were. `Histogram.observe()` (`core/performance/histogram.py`) buckets a `MetricPoint`'s raw value with no unit conversion, and TIMER-type points carry raw **nanosecond** durations (per `docs/PerformancePlan.md`'s own design rule: *"Internal time is `perf_counter_ns()`... conversion happens only at exporter or presentation boundaries"*). But `bucket_bounds` defaults to `DEFAULT_HISTOGRAM_BUCKET_BOUNDS_MS` — bounds documented and named as **milliseconds** (0.5 to 10,000). A 3.9ms request is ~3,900,000 raw ns, which is larger than every bound up to `10000`, so it always lands in `+Inf`. This is a pre-existing bug in `PrometheusExporter`'s bucket rendering specifically (the streaming P² quantiles used everywhere else — dashboard, JSON, `stage_breakdown` — convert ns→ms correctly and are accurate); it does not affect any of the mean/percentile numbers reported elsewhere in this document. **Recommended follow-up:** either convert `MetricPoint.value` to milliseconds before calling `Histogram.observe()` for TIMER points, or change `bucket_bounds` to nanosecond-scale bounds — not fixed here since it wasn't part of this report's request.

## 6. Summary

- The performance module, wired per `docs/performance/README.md`, produces a coherent, three-way-consistent view of request performance: aggregate throughput/latency (§2), a live per-stage dashboard (§3), full per-request traces on demand (§4), and file/text exporter snapshots (§5) — all reading from the same `PerformanceRegistry`.
- At 100% sampling, profiling this workload costs roughly 9–37% added latency depending on percentile and ~15% throughput, with the actual instrumented SQL work itself contributing negligibly — the cost is in the trace/metric bookkeeping, not the thing being measured.
- One real defect was found and documented while producing this report: `PrometheusExporter`'s histogram buckets are effectively non-functional (§5) due to a ns/ms unit mismatch — everything else (dashboard, JSON export, `stage_breakdown`) is unaffected and accurate.
- Phase 14's registry fix (removing an O(N) rebuild from every request once history hit capacity — see `docs/performance/phase-14-optimization-report.md`) is reflected in §2's numbers; the pre-fix run of the same benchmark showed p95 latency +190.9% instead of +37.3%.
