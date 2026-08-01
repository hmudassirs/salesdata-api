"""Process memory usage collector.

Reads two independent sources, each optional:

- `resource.getrusage(RUSAGE_SELF).ru_maxrss`: peak resident set size
  since process start. Units differ by platform (kibibytes on Linux,
  bytes on macOS/BSD) — normalized to bytes here so the emitted gauge
  is portable. `resource` does not exist on Windows, so its absence is
  handled, not raised.
- `/proc/self/status`'s `VmRSS` line: *current* resident set size,
  Linux-only. Complements `ru_maxrss`, which never decreases.

If `tracemalloc` is running (`tracemalloc.is_tracing()`), its current
and peak traced-allocation sizes are included too — this is opt-in by
the caller having started `tracemalloc` themselves; this collector
never starts or stops it.
"""

from __future__ import annotations

import sys
import tracemalloc
from pathlib import Path

from core.performance.collectors._util import gauge_point
from core.performance.enums import MetricUnit
from core.performance.metric import MetricPoint

try:
    import resource
except ImportError:  # pragma: no cover - non-POSIX platforms (e.g. Windows)
    resource = None  # type: ignore[assignment]

_PROC_STATUS_PATH = Path("/proc/self/status")


class MemoryCollector:
    """Sample process memory usage as a small set of gauges."""

    name = "memory"

    def collect(self) -> list[MetricPoint]:
        """Return whatever memory gauges are available on this platform."""
        points: list[MetricPoint] = []
        points.extend(self._peak_rss_point())
        points.extend(self._current_rss_point())
        points.extend(self._tracemalloc_points())
        return points

    @staticmethod
    def _peak_rss_point() -> list[MetricPoint]:
        if resource is None:
            return []
        try:
            raw = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        except (OSError, ValueError):
            return []
        # ru_maxrss is KiB on Linux, bytes on macOS/BSD.
        multiplier = 1 if sys.platform == "darwin" else 1024
        return [
            gauge_point(
                "process_memory_peak_rss_bytes",
                raw * multiplier,
                unit=MetricUnit.BYTES,
            )
        ]

    @staticmethod
    def _current_rss_point() -> list[MetricPoint]:
        try:
            text = _PROC_STATUS_PATH.read_text()
        except OSError:
            return []
        for line in text.splitlines():
            if line.startswith("VmRSS:"):
                parts = line.split()
                if len(parts) >= 2 and parts[1].isdigit():  # noqa: PLR2004
                    return [
                        gauge_point(
                            "process_memory_current_rss_bytes",
                            int(parts[1]) * 1024,
                            unit=MetricUnit.BYTES,
                        )
                    ]
        return []

    @staticmethod
    def _tracemalloc_points() -> list[MetricPoint]:
        if not tracemalloc.is_tracing():
            return []
        current, peak = tracemalloc.get_traced_memory()
        return [
            gauge_point(
                "process_memory_tracemalloc_current_bytes",
                current,
                unit=MetricUnit.BYTES,
            ),
            gauge_point(
                "process_memory_tracemalloc_peak_bytes", peak, unit=MetricUnit.BYTES
            ),
        ]
