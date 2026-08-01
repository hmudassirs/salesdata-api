"""Garbage-collector statistics collector.

Reads `gc.get_count()` (current object counts per generation, cheap and
always available) and, when `gc.get_stats()` reports them (CPython
3.x), the cumulative per-generation `collections`/`collected`/
`uncollectable` counters. Never triggers a collection itself — this is
observation only.
"""

from __future__ import annotations

import gc

from core.performance.collectors._util import gauge_point
from core.performance.metric import MetricPoint


class GCCollector:
    """Sample per-generation garbage-collector counters as gauges."""

    name = "gc"

    def collect(self) -> list[MetricPoint]:
        """Return current object counts and cumulative collector stats."""
        points: list[MetricPoint] = []
        for generation, count in enumerate(gc.get_count()):
            points.append(
                gauge_point(
                    "gc_object_count",
                    count,
                    tags={"generation": str(generation)},
                )
            )
        for generation, stats in enumerate(gc.get_stats()):
            for field in ("collections", "collected", "uncollectable"):
                value = stats.get(field)
                if value is None:
                    continue
                points.append(
                    gauge_point(
                        f"gc_{field}_total",
                        value,
                        tags={"generation": str(generation)},
                    )
                )
        return points
