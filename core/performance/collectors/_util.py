"""Internal helper shared by the resource collectors in this package."""

from __future__ import annotations

from core.performance.clock import PerformanceClock
from core.performance.enums import MetricType, MetricUnit
from core.performance.metric import MetricPoint
from core.performance.types import MetricName, MetricValue, Tags


def gauge_point(
    name: str,
    value: MetricValue,
    unit: MetricUnit = MetricUnit.COUNT,
    tags: Tags | None = None,
) -> MetricPoint:
    """Build one gauge `MetricPoint` stamped with the current clock reading."""
    return MetricPoint(
        name=MetricName(name),
        metric_type=MetricType.GAUGE,
        value=value,
        timestamp_ns=PerformanceClock.now_ns(),
        unit=unit,
        tags={} if tags is None else tags,
    )
