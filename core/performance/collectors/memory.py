"""Process memory usage collector.

Reads platform-appropriate sources for resident set size, since none of
Python's cross-platform stdlib covers this on every OS:

- POSIX (Linux/macOS/BSD): `resource.getrusage(RUSAGE_SELF).ru_maxrss`
  (peak RSS since process start; units differ by platform — kibibytes on
  Linux, bytes on macOS/BSD, normalized to bytes here) plus, on Linux
  only, `/proc/self/status`'s `VmRSS` line for *current* (not just peak)
  RSS.
- Windows: neither `resource` nor `/proc` exist, so both of the above
  silently produce nothing there (by original design: "degrade to `[]`,
  never raise"). Instead this reads `GetProcessMemoryInfo` from
  `psapi.dll` via `ctypes` — stdlib-only, no extra dependency — for both
  current (`WorkingSetSize`) and peak (`PeakWorkingSetSize`) RSS
  equivalents.

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

_IS_WINDOWS = sys.platform == "win32"

if _IS_WINDOWS:  # pragma: no cover - exercised only on Windows runners
    import ctypes
    from ctypes import wintypes

    class _PROCESS_MEMORY_COUNTERS(ctypes.Structure):
        _fields_ = (
            ("cb", wintypes.DWORD),
            ("PageFaultCount", wintypes.DWORD),
            ("PeakWorkingSetSize", ctypes.c_size_t),
            ("WorkingSetSize", ctypes.c_size_t),
            ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
            ("QuotaPagedPoolUsage", ctypes.c_size_t),
            ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
            ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
            ("PagefileUsage", ctypes.c_size_t),
            ("PeakPagefileUsage", ctypes.c_size_t),
        )

    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _psapi = ctypes.WinDLL("psapi", use_last_error=True)

    # Explicit argtypes/restype are required here, not cosmetic: without
    # them ctypes defaults to marshaling the HANDLE and pointer arguments
    # as 32-bit `int`, which on 64-bit Windows corrupts the pseudo-handle
    # from GetCurrentProcess() and makes GetProcessMemoryInfo silently
    # return failure (0) rather than raising — indistinguishable from
    # "unavailable" without this fix.
    _kernel32.GetCurrentProcess.restype = wintypes.HANDLE
    _kernel32.GetCurrentProcess.argtypes = []

    _psapi.GetProcessMemoryInfo.restype = wintypes.BOOL
    _psapi.GetProcessMemoryInfo.argtypes = [
        wintypes.HANDLE,
        ctypes.POINTER(_PROCESS_MEMORY_COUNTERS),
        wintypes.DWORD,
    ]


class MemoryCollector:
    """Sample process memory usage as a small set of gauges."""

    name = "memory"

    def collect(self) -> list[MetricPoint]:
        """Return whatever memory gauges are available on this platform."""
        points: list[MetricPoint] = []
        if _IS_WINDOWS:
            points.extend(self._windows_rss_points())
        else:
            points.extend(self._peak_rss_point())
            points.extend(self._current_rss_point())
        points.extend(self._tracemalloc_points())
        return points

    @staticmethod
    def _windows_rss_points() -> list[MetricPoint]:  # pragma: no cover - Windows-only
        """Read current + peak working-set size via psapi's GetProcessMemoryInfo.

        `WorkingSetSize`/`PeakWorkingSetSize` are Windows' nearest
        equivalents to Linux's current/peak RSS — same emitted gauge
        names as the POSIX path, so the dashboard/exporters don't need
        to know which platform produced them. Any failure (missing
        DLL, unexpected struct layout, permissions) degrades to `[]`
        rather than raising, matching every other collector's contract.
        """
        try:
            counters = _PROCESS_MEMORY_COUNTERS()
            counters.cb = ctypes.sizeof(_PROCESS_MEMORY_COUNTERS)
            handle = _kernel32.GetCurrentProcess()
            ok = _psapi.GetProcessMemoryInfo(
                handle, ctypes.byref(counters), counters.cb
            )
            if not ok:
                return []
        except (OSError, AttributeError, ValueError):
            return []
        return [
            gauge_point(
                "process_memory_current_rss_bytes",
                counters.WorkingSetSize,
                unit=MetricUnit.BYTES,
            ),
            gauge_point(
                "process_memory_peak_rss_bytes",
                counters.PeakWorkingSetSize,
                unit=MetricUnit.BYTES,
            ),
        ]

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
