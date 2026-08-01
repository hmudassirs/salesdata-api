"""Process-identity and file-descriptor collector.

Reports the process id and, on Linux (via `/proc/self/fd`, the same
source `lsof`/`ls -l /proc/<pid>/fd` use), the current and maximum
open file-descriptor counts — a common exhaustion point for a
connection-pooled service under load. Degrades to just the pid gauge
on platforms without `/proc`.
"""

from __future__ import annotations

import os
from pathlib import Path

from core.performance.collectors._util import gauge_point
from core.performance.metric import MetricPoint

try:
    import resource
except ImportError:  # pragma: no cover - non-POSIX platforms (e.g. Windows)
    resource = None  # type: ignore[assignment]

_PROC_FD_DIR = Path("/proc/self/fd")


class ProcessCollector:
    """Sample process id and open file-descriptor counts as gauges."""

    name = "process"

    def collect(self) -> list[MetricPoint]:
        """Return the pid gauge plus open/max file-descriptor gauges if available."""
        points = [gauge_point("process_id", os.getpid())]
        points.extend(self._open_fd_points())
        points.extend(self._max_fd_points())
        return points

    @staticmethod
    def _open_fd_points() -> list[MetricPoint]:
        try:
            open_fds = len(list(_PROC_FD_DIR.iterdir()))
        except OSError:
            return []
        return [gauge_point("process_open_fds", open_fds)]

    @staticmethod
    def _max_fd_points() -> list[MetricPoint]:
        if resource is None:
            return []
        try:
            soft_limit, _hard_limit = resource.getrlimit(resource.RLIMIT_NOFILE)
        except (OSError, ValueError):
            return []
        return [gauge_point("process_max_fds", soft_limit)]
