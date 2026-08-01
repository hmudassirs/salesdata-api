"""Flatten a `PerformanceRegistry`'s aggregate state into one plain shape.

Every exporter in this package (JSON, CSV, console, Prometheus, OTel)
needs the same three lists — current counters, current gauges, and
current histogram snapshots — and none of them should re-implement
reading `registry.counters()`/`gauges()`/`histograms()`. `build_snapshot`
is that one shared read; each exporter only has to turn
`list[MetricSnapshot]` into its own external format.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

from core.performance.types import Tags

if TYPE_CHECKING:
    from core.performance.registry import PerformanceRegistry

SnapshotKind = Literal["counter", "gauge", "histogram"]


@dataclass(frozen=True, slots=True)
class MetricSnapshot:
    """One flattened metric, taken at the moment `build_snapshot` ran.

    `value` is populated for `"counter"`/`"gauge"` kinds; `histogram`
    (the `StreamingHistogram.snapshot()` dict: count/sum/mean/min/max
    and each tracked quantile) is populated for `"histogram"` kind.
    Exactly one of the two is set, matching which kind this is.
    """

    name: str
    kind: SnapshotKind
    tags: Tags
    value: float | None = None
    histogram: dict[str, float | int | None] | None = None


def build_snapshot(registry: PerformanceRegistry) -> list[MetricSnapshot]:
    """Read every current counter, gauge, and histogram from `registry`."""
    snapshots: list[MetricSnapshot] = []
    for counter in registry.counters():
        snapshots.append(
            MetricSnapshot(
                name=counter.name, kind="counter", tags=dict(counter.tags),
                value=counter.value,
            )
        )
    for gauge in registry.gauges():
        snapshots.append(
            MetricSnapshot(
                name=gauge.name, kind="gauge", tags=dict(gauge.tags),
                value=gauge.value,
            )
        )
    for histogram in registry.histograms():
        snapshots.append(
            MetricSnapshot(
                name=histogram.name,
                kind="histogram",
                tags=dict(histogram.tags),
                histogram=histogram.snapshot(),
            )
        )
    return snapshots
