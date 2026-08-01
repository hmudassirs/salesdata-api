#!/usr/bin/env python3
"""Benchmark the core profiling primitives' own overhead.

Compares three named runs of the same simulated request-handling work
(three nested `stage()` calls plus a counter and a gauge emission):

- `no-profiler`  : the simulated work runs with no profiler involved at
                   all, giving a wall-clock floor for the work itself.
- `null-profiler`: every call goes through `NullRequestProfiler` — the
                   disabled path every adapter in this codebase falls
                   back to. Should cost close to nothing extra.
- `real-profiler`: every call goes through a real `RequestProfiler` —
                   trace nodes are opened/closed and `MetricPoint`s are
                   allocated.

The gap between `null-profiler` and `no-profiler` is the fixed cost of
routing through the disabled-path interface at all; the gap between
`real-profiler` and `null-profiler` is what enabling profiling actually
costs per request.

Usage:
    python -m benchmarks.benchmark_timer
    python -m benchmarks.benchmark_timer --iterations 5000 --json-out out.json
"""

from __future__ import annotations

from benchmarks._common import (
    build_arg_parser,
    render_report,
    run_benchmark,
    write_json_results,
)
from core.performance.enums import PerformanceStage
from core.performance.request_profiler import (
    AnyProfiler,
    NullRequestProfiler,
    RequestProfiler,
)
from core.performance.types import MetricName


def _simulate_work_no_profiler() -> None:
    total = 0
    for _ in range(3):
        for i in range(100):
            total += i
    _ = total


def _simulate_work_with_profiler(profiler: AnyProfiler) -> None:
    with profiler.stage(PerformanceStage.POOL_ACQUIRE, MetricName("pool_acquire")):
        with profiler.stage(PerformanceStage.SQL_EXECUTE, MetricName("sql_execute")):
            total = 0
            for i in range(100):
                total += i
            profiler.counter(MetricName("rows_returned"), total % 7)
        profiler.gauge(MetricName("pool_active_connections"), 1)


def _run_with_null_profiler() -> None:
    _simulate_work_with_profiler(NullRequestProfiler())


def _run_with_real_profiler() -> None:
    profiler = RequestProfiler()
    _simulate_work_with_profiler(profiler)
    profiler.complete()


def main() -> None:
    parser = build_arg_parser(__doc__ or "")
    args = parser.parse_args()

    results = [
        run_benchmark("no-profiler", args.iterations, _simulate_work_no_profiler),
        run_benchmark("null-profiler", args.iterations, _run_with_null_profiler),
        run_benchmark("real-profiler", args.iterations, _run_with_real_profiler),
    ]

    print(render_report(results))  # noqa: T201
    if args.json_out:
        write_json_results(args.json_out, results)


if __name__ == "__main__":
    main()
