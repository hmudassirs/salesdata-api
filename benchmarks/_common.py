"""Shared result type and reporting helpers for the `benchmarks/` scripts.

Every `benchmark_*.py` script in this directory produces one or more
named `BenchmarkResult`s (e.g. "uninstrumented" vs "instrumented", or
"baseline" vs "candidate") the same way, so throughput/latency/error
computation and report formatting live here once rather than once per
script.

Not part of `core.performance` itself — these are standalone scripts
that *exercise* the real subsystems end to end (a real `SyncConnectionPool`,
a real temp-file SQLite database, a real FastAPI app), the way
`load_test.py` already exercises the live API. `docs/PerformancePlan.md`'s
package boundary is about `core/performance/`'s internal layering; it does
not cover these scripts.
"""

from __future__ import annotations

import argparse
import json
import statistics
import time
from collections import defaultdict
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, cast

if TYPE_CHECKING:
    from core.performance.registry import PerformanceRegistry
    from core.performance.trace import TraceNode

_PERCENTILES = (50, 90, 95, 99)


@dataclass(frozen=True, slots=True)
class BenchmarkResult:
    """One named run's timing/error/throughput data.

    `durations_seconds` holds one entry per call (wall-clock, via
    `time.perf_counter`), in the order calls completed — that's what
    lets `percentile()` and the mean/min/max in `to_dict()` be computed
    after the fact instead of tracked incrementally.
    """

    name: str
    iterations: int
    total_seconds: float
    errors: int
    durations_seconds: tuple[float, ...] = field(repr=False)

    @property
    def throughput_per_second(self) -> float:
        """Completed calls per wall-clock second across the whole run."""
        return self.iterations / self.total_seconds if self.total_seconds > 0 else 0.0

    @property
    def error_rate(self) -> float:
        """Fraction of calls (0.0-1.0) that raised."""
        return self.errors / self.iterations if self.iterations else 0.0

    def percentile(self, pct: float) -> float:
        """Return the `pct`th percentile call duration, in seconds."""
        if not self.durations_seconds:
            return 0.0
        ordered = sorted(self.durations_seconds)
        index = min(
            len(ordered) - 1, max(0, round(pct / 100 * (len(ordered) - 1)))
        )
        return ordered[index]

    def to_dict(self) -> dict[str, object]:
        """Render a JSON-friendly summary: throughput, errors, latency_ms."""
        durations = self.durations_seconds
        latency_ms = {
            "mean": (statistics.fmean(durations) if durations else 0.0) * 1000,
            "min": (min(durations) if durations else 0.0) * 1000,
            "max": (max(durations) if durations else 0.0) * 1000,
        }
        for pct in _PERCENTILES:
            latency_ms[f"p{pct}"] = self.percentile(pct) * 1000
        return {
            "name": self.name,
            "iterations": self.iterations,
            "errors": self.errors,
            "error_rate": self.error_rate,
            "total_seconds": self.total_seconds,
            "throughput_per_second": self.throughput_per_second,
            "latency_ms": latency_ms,
        }


def run_benchmark(
    name: str, iterations: int, call: Callable[[], None]
) -> BenchmarkResult:
    """Run `call()` `iterations` times, timing each call and counting errors.

    An exception from `call()` is caught, counted, and does not stop the
    run — matching how a real load test tallies error *rate* rather than
    aborting on the first failure.
    """
    durations: list[float] = []
    errors = 0
    run_start = time.perf_counter()
    for _ in range(iterations):
        call_start = time.perf_counter()
        try:
            call()
        except Exception:
            errors += 1
        durations.append(time.perf_counter() - call_start)
    total_seconds = time.perf_counter() - run_start
    return BenchmarkResult(
        name=name,
        iterations=iterations,
        total_seconds=total_seconds,
        errors=errors,
        durations_seconds=tuple(durations),
    )


def render_report(results: Iterable[BenchmarkResult]) -> str:
    """Render one or more results as an aligned, human-readable text block."""
    lines: list[str] = []
    for result in results:
        d = result.to_dict()
        latency = cast("dict[str, float]", d["latency_ms"])
        lines.append(f"{result.name}:")
        lines.append(f"  iterations : {d['iterations']}")
        lines.append(f"  errors     : {d['errors']} ({d['error_rate']:.2%})")
        lines.append(f"  throughput : {d['throughput_per_second']:.1f} ops/sec")
        lines.append(
            "  latency ms : "
            f"mean={latency['mean']:.3f} p50={latency['p50']:.3f} "
            f"p90={latency['p90']:.3f} p95={latency['p95']:.3f} "
            f"p99={latency['p99']:.3f} min={latency['min']:.3f} "
            f"max={latency['max']:.3f}"
        )
    return "\n".join(lines)


def compare(baseline: BenchmarkResult, candidate: BenchmarkResult) -> str:
    """Render a before/after comparison of a named baseline and candidate run."""

    def pct_change(old: float, new: float) -> str:
        if old == 0:
            return "n/a"
        return f"{(new - old) / old * 100:+.1f}%"

    lines = [f"{baseline.name} (baseline) vs {candidate.name} (candidate):"]
    throughput_change = pct_change(
        baseline.throughput_per_second, candidate.throughput_per_second
    )
    lines.append(
        "  throughput : "
        f"{baseline.throughput_per_second:.1f} -> {candidate.throughput_per_second:.1f}"
        f" ops/sec ({throughput_change})"
    )
    for pct in _PERCENTILES:
        before_ms = baseline.percentile(pct) * 1000
        after_ms = candidate.percentile(pct) * 1000
        lines.append(
            f"  p{pct} latency : {before_ms:.3f}ms -> {after_ms:.3f}ms "
            f"({pct_change(before_ms, after_ms)})"
        )
    lines.append(
        f"  errors     : {baseline.errors}/{baseline.iterations} -> "
        f"{candidate.errors}/{candidate.iterations}"
    )
    return "\n".join(lines)


def stage_breakdown(registry: PerformanceRegistry) -> dict[str, dict[str, float]]:
    """Summarize mean/p95 duration (ms) per trace stage across retained history.

    Walks every retained `RequestProfile`'s trace tree (see
    `request_profiler.RequestProfile`/`trace.TraceNode`), grouping node
    durations by trace-node name (e.g. `"pool_acquire"`, `"sql_execute"`)
    regardless of which request or how deep in the tree they occurred.
    """
    samples: dict[str, list[float]] = defaultdict(list)
    for profile in registry.history():
        if profile.trace is None or profile.trace.root is None:
            continue
        _collect_node_durations(profile.trace.root, samples)
    breakdown: dict[str, dict[str, float]] = {}
    for name, durations_ns in samples.items():
        durations_ms = [d / 1_000_000 for d in durations_ns]
        ordered = sorted(durations_ms)
        p95_index = min(len(ordered) - 1, max(0, round(0.95 * (len(ordered) - 1))))
        breakdown[name] = {
            "count": len(durations_ms),
            "mean_ms": statistics.fmean(durations_ms),
            "p95_ms": ordered[p95_index],
        }
    return breakdown


def _collect_node_durations(node: TraceNode, samples: dict[str, list[float]]) -> None:
    if node.duration_ns is not None:
        samples[node.name].append(node.duration_ns)
    for child in node.children:
        _collect_node_durations(child, samples)


def render_stage_breakdown(breakdown: dict[str, dict[str, float]]) -> str:
    """Render `stage_breakdown`'s output as an aligned text block."""
    if not breakdown:
        return "  (no retained request history)"
    lines = []
    for name, stats in sorted(breakdown.items(), key=lambda kv: -kv[1]["mean_ms"]):
        lines.append(
            f"  {name:<24} count={stats['count']:<6.0f} "
            f"mean={stats['mean_ms']:.3f}ms p95={stats['p95_ms']:.3f}ms"
        )
    return "\n".join(lines)


def build_arg_parser(description: str) -> argparse.ArgumentParser:
    """Return the `argparse.ArgumentParser` shared by every benchmark script."""
    parser = argparse.ArgumentParser(description=description)
    parser.add_argument(
        "--iterations", type=int, default=2000, help="Calls per named run."
    )
    parser.add_argument(
        "--json-out",
        type=str,
        default=None,
        help="Optional path to write JSON results to.",
    )
    return parser


def write_json_results(path: str, results: Iterable[BenchmarkResult]) -> None:
    """Write every result's `to_dict()` to `path` as one JSON array."""
    with open(path, "w") as f:  # noqa: PTH123
        json.dump([r.to_dict() for r in results], f, indent=2, sort_keys=True)
