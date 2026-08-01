#!/usr/bin/env python3
"""Benchmark `core.db.pool.SyncConnectionPool` acquire/release throughput.

Compares:

- `raw-pool`         : call the real `SyncConnectionPool` directly.
- `instrumented-pool`: the same pool wrapped in
                        `core.performance.adapters.pool.SyncPoolTimingAdapter`,
                        with no profiler bound (the common case: most
                        requests are unsampled).
- `instrumented-pool-profiled`: the same wrapped pool, but with a real
                        `RequestProfiler` bound via `bind_profiler` for
                        every call — the cost paid by a *sampled* request.

`raw-pool` vs `instrumented-pool` isolates the adapter's own dispatch
overhead (profiler lookup + no-op branch) on the unsampled path;
`instrumented-pool` vs `instrumented-pool-profiled` isolates what
actually timing pool acquire/release costs once sampled.

Usage:
    python -m benchmarks.benchmark_pool
    python -m benchmarks.benchmark_pool --iterations 5000
"""

from __future__ import annotations

from benchmarks._common import (
    build_arg_parser,
    render_report,
    run_benchmark,
    write_json_results,
)
from core.db.pool.sync_pool import SyncConnectionPool
from core.performance.adapters.pool import SyncPoolTimingAdapter
from core.performance.context import bind_profiler
from core.performance.request_profiler import RequestProfiler


def _make_pool() -> SyncConnectionPool:
    return SyncConnectionPool(
        create_connection=object, min_conn=4, max_conn=16, timeout=5
    )


def _acquire_release(pool: SyncConnectionPool | SyncPoolTimingAdapter) -> None:
    conn = pool.acquire()
    pool.release(conn)


def main() -> None:
    parser = build_arg_parser(__doc__ or "")
    args = parser.parse_args()

    raw_pool = _make_pool()
    instrumented_pool = SyncPoolTimingAdapter(_make_pool())
    profiled_pool = SyncPoolTimingAdapter(_make_pool())

    def _run_profiled() -> None:
        with bind_profiler(RequestProfiler()):
            _acquire_release(profiled_pool)

    results = [
        run_benchmark("raw-pool", args.iterations, lambda: _acquire_release(raw_pool)),
        run_benchmark(
            "instrumented-pool",
            args.iterations,
            lambda: _acquire_release(instrumented_pool),
        ),
        run_benchmark("instrumented-pool-profiled", args.iterations, _run_profiled),
    ]

    print(render_report(results))  # noqa: T201
    if args.json_out:
        write_json_results(args.json_out, results)


if __name__ == "__main__":
    main()
