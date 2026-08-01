# Configuration reference

`core.performance.config.PerformanceConfig` is a frozen dataclass. Every
field has a safe, disabled-by-default default, so `PerformanceConfig()`
with no arguments is always valid and costs nothing at runtime.

| Field | Default | Env var | Notes |
|---|---|---|---|
| `enabled` | `False` | `PERF_ENABLED` | Master switch. `False` means `should_sample()` always returns `False` without touching the random source, and the FastAPI middleware binds a `NullRequestProfiler` — zero timing, allocation, or trace work. |
| `sample_rate_percent` | `100` | `PERF_SAMPLE_RATE_PERCENT` | `0`-`100`. `100`/`0` skip the RNG entirely (always/never sample); values in between call `random.randint(1, 100) <= sample_rate_percent` once per request. |
| `max_trace_nodes` | `256` | `PERF_MAX_TRACE_NODES` | Upper bound on nodes in one request's trace tree (validated `>= 1` at construction). |
| `max_request_history` | `1000` | `PERF_MAX_REQUEST_HISTORY` | Size of the registry's bounded ring buffer of completed `RequestProfile`s (what `/debug/request/{id}` and `benchmarks.stage_breakdown` read from). `0` makes history **unbounded** rather than disabling it — `PerformanceRegistry.__post_init__` builds `deque(maxlen=self.max_request_history or None)`, and `0 or None` is `None` (no `maxlen`) — so set this to a real positive number in production; `0` will retain every completed request's full trace forever. |
| `collect_memory` | `False` | *(none)* | Enables `collectors.MemoryCollector` via `build_enabled_collectors`. |
| `collect_gc` | `False` | *(none)* | Enables `collectors.GCCollector`. |
| `collect_threads` | `False` | *(none)* | Enables `collectors.ThreadCollector`. |
| `collect_cpu` | `False` | *(none)* | Enables `collectors.CPUCollector`. |
| `collect_asyncio` | `False` | *(none)* | Enables `collectors.AsyncioCollector`. |
| `collect_process` | `False` | *(none)* | Enables `collectors.ProcessCollector`. |

The six `collect_*` flags have no environment variable yet (a gap that
predates Phase 9 — `PerformanceConfig.from_env()` doesn't read them);
set them via the constructor directly, e.g.
`PerformanceConfig.from_env()` won't turn on collectors, so build one
explicitly if you need them:

```python
from dataclasses import replace
from core.performance.config import PerformanceConfig

config = replace(PerformanceConfig.from_env(), collect_memory=True, collect_gc=True)
```

An invalid value (`sample_rate_percent` outside `0`-`100`,
`max_trace_nodes < 1`, or `max_request_history < 0`) raises
`PerformanceConfigurationError` at construction time — invalid
configuration fails fast rather than degrading silently.

## Sampling semantics

`should_sample()` is called once per request by the FastAPI middleware.
It is a plain coin flip per request, independent of any other request —
there is no fixed-window or reservoir sampling. If you need "one in
every N requests exactly" or session-consistent sampling, that is not
provided here and would need to sit in front of `should_sample()`.

## Disabled-path guarantee

Per `docs/PerformancePlan.md`'s design rules, `enabled=False` (or a
request that loses the sampling coin flip) is a true no-op end to end:

- The middleware attaches a `NullRequestProfiler` to `request.state` for
  endpoint code that calls `get_request_profiler(request)` directly, but
  only binds the context-local "current profiler"
  (`core.performance.context.bind_profiler`) for *sampled* requests. An
  unsampled request never binds anything, so `get_current_profiler()`
  returns `None` for its whole lifetime.
- Every adapter in `adapters/` checks `get_current_profiler() is None`
  and, when it is, delegates directly to the wrapped subsystem with no
  timing wrapper — one `is None` check, no allocation, no extra call.
- `benchmarks/benchmark_timer.py` and `benchmarks/benchmark_pool.py`
  measure exactly this gap (`no-profiler`/`null-profiler` vs
  `real-profiler`) — see `benchmarking.md`.
