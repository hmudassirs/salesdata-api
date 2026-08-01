"""Thread-count collector.

Reports the number of live `threading` threads, split into daemon and
non-daemon counts. Uses `threading.enumerate()`, which only sees
threads created through the `threading` module (matching what
`threading.active_count()` counts) — it cannot see raw OS-level
threads started outside of Python.
"""

from __future__ import annotations

import threading

from core.performance.collectors._util import gauge_point
from core.performance.metric import MetricPoint


class ThreadCollector:
    """Sample live thread counts as gauges."""

    name = "threads"

    def collect(self) -> list[MetricPoint]:
        """Return total, daemon, and non-daemon live thread-count gauges."""
        current_threads = threading.enumerate()
        daemon_count = sum(1 for t in current_threads if t.daemon)
        return [
            gauge_point("thread_count_total", len(current_threads)),
            gauge_point("thread_count_daemon", daemon_count),
            gauge_point("thread_count_non_daemon", len(current_threads) - daemon_count),
        ]
