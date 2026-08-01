"""Optional, process-wide resource collectors (Phase 9).

Each collector implements `ResourceCollector` (see `base.py`): a
`collect()` method returning a snapshot of gauge `MetricPoint`s for one
resource dimension. None of them are wired in automatically — see
`scheduler.py` for the opt-in helper that builds the subset enabled by
a `PerformanceConfig`, and the periodic runner that feeds their output
straight into a `PerformanceRegistry`'s aggregator.
"""

from __future__ import annotations

from core.performance.collectors.asyncio_collector import AsyncioCollector
from core.performance.collectors.base import ResourceCollector
from core.performance.collectors.cpu import CPUCollector
from core.performance.collectors.gc_collector import GCCollector
from core.performance.collectors.memory import MemoryCollector
from core.performance.collectors.process import ProcessCollector
from core.performance.collectors.scheduler import (
    CollectorScheduler,
    build_enabled_collectors,
    collect_all,
)
from core.performance.collectors.threads import ThreadCollector

__all__ = [
    "AsyncioCollector",
    "CPUCollector",
    "CollectorScheduler",
    "GCCollector",
    "MemoryCollector",
    "ProcessCollector",
    "ResourceCollector",
    "ThreadCollector",
    "build_enabled_collectors",
    "collect_all",
]
