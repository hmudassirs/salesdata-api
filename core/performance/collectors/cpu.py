"""Process CPU-time collector.

Reports cumulative user/system CPU seconds consumed by this process
since it started, via `os.times()` — deliberately a running total
rather than an instantaneous percentage, matching how Prometheus's own
`process_cpu_seconds_total` works: a percentage requires a time
window, which is a property of two samples plus the interval between
them, not of one collector call. Callers that want a percentage (e.g.
an exporter or dashboard) take the difference between two snapshots
and divide by the elapsed wall-clock time themselves.
"""

from __future__ import annotations

import os

from core.performance.collectors._util import gauge_point
from core.performance.enums import MetricUnit
from core.performance.metric import MetricPoint


class CPUCollector:
    """Sample cumulative process CPU time and logical CPU count as gauges."""

    name = "cpu"

    def collect(self) -> list[MetricPoint]:
        """Return cumulative user/system CPU-second gauges and CPU count."""
        times = os.times()
        points = [
            gauge_point(
                "process_cpu_user_seconds_total", times.user, unit=MetricUnit.SECONDS
            ),
            gauge_point(
                "process_cpu_system_seconds_total",
                times.system,
                unit=MetricUnit.SECONDS,
            ),
        ]
        cpu_count = os.cpu_count()
        if cpu_count is not None:
            points.append(gauge_point("cpu_logical_count", cpu_count))
        return points
