"""Monotonic nanosecond clock used by performance instrumentation."""

from __future__ import annotations

from time import perf_counter_ns

from .types import TimestampNS


class PerformanceClock:
    """Provide monotonic timestamps for internal performance measurements."""

    @staticmethod
    def now_ns() -> TimestampNS:
        """Return the current monotonic timestamp as integer nanoseconds."""
        return TimestampNS(perf_counter_ns())
