"""Asyncio task-count collector.

`asyncio.all_tasks()` requires a running event loop; called from
synchronous code (e.g. a sync debug-dashboard handler, or a benchmark
running outside asyncio) it raises `RuntimeError`, which this collector
treats the same as "nothing to report" rather than propagating, per
`ResourceCollector`'s "never break the caller" contract.
"""

from __future__ import annotations

import asyncio

from core.performance.collectors._util import gauge_point
from core.performance.metric import MetricPoint


class AsyncioCollector:
    """Sample the running event loop's live/pending task counts as gauges."""

    name = "asyncio"

    def collect(self) -> list[MetricPoint]:
        """Return task-count gauges, or `[]` if no event loop is running."""
        try:
            tasks = asyncio.all_tasks()
        except RuntimeError:
            return []

        pending = sum(1 for task in tasks if not task.done())
        return [
            gauge_point("asyncio_task_count_total", len(tasks)),
            gauge_point("asyncio_task_count_pending", pending),
        ]
