"""Wiring and periodic scheduling for `ResourceCollector`s.

`build_enabled_collectors` turns a `PerformanceConfig`'s `collect_*`
flags into the matching list of collector instances — the same
"absence of explicit configuration degrades to a safe, disabled-by-
default state" pattern `middleware/fastapi.py` uses for the profiler
itself. `collect_all` runs a list of collectors once and flattens their
output. `CollectorScheduler` is the optional background runner: it
calls `collect_all` on a fixed interval and feeds the result straight
into a `PerformanceRegistry`'s aggregator via `ingest`, entirely outside
any request path, matching `docs/PerformancePlan.md`'s "exporters and
aggregation are not allowed to block request handling" rule.
"""

from __future__ import annotations

import asyncio
import contextlib

from core.performance.collectors.asyncio_collector import AsyncioCollector
from core.performance.collectors.base import ResourceCollector
from core.performance.collectors.cpu import CPUCollector
from core.performance.collectors.gc_collector import GCCollector
from core.performance.collectors.memory import MemoryCollector
from core.performance.collectors.process import ProcessCollector
from core.performance.collectors.threads import ThreadCollector
from core.performance.config import PerformanceConfig
from core.performance.metric import MetricPoint
from core.performance.registry import PerformanceRegistry

_DEFAULT_INTERVAL_SECONDS = 15.0


def build_enabled_collectors(config: PerformanceConfig) -> list[ResourceCollector]:
    """Return the collectors this config's `collect_*` flags turn on.

    Each flag maps to exactly one collector, in the same order the
    fields appear on `PerformanceConfig`. An all-`False` config (the
    default) returns an empty list — no collector runs unless
    explicitly enabled.
    """
    collectors: list[ResourceCollector] = []
    if config.collect_memory:
        collectors.append(MemoryCollector())
    if config.collect_gc:
        collectors.append(GCCollector())
    if config.collect_threads:
        collectors.append(ThreadCollector())
    if config.collect_cpu:
        collectors.append(CPUCollector())
    if config.collect_asyncio:
        collectors.append(AsyncioCollector())
    if config.collect_process:
        collectors.append(ProcessCollector())
    return collectors


def collect_all(collectors: list[ResourceCollector]) -> list[MetricPoint]:
    """Run every collector once and flatten their points into one list."""
    points: list[MetricPoint] = []
    for collector in collectors:
        points.extend(collector.collect())
    return points


class CollectorScheduler:
    """Periodically sample a set of collectors into a registry's aggregator.

    Deliberately not started automatically by anything in this
    package: constructing one has no effect until `start()` is called,
    and `stop()` always leaves the registry's already-ingested state
    intact. Intended for application startup code (a `LifecycleStep`,
    in this codebase's terms) to own explicitly, the same way
    `ObservabilityWriteQueue`'s background worker is started/stopped by
    the application lifespan rather than at import time.
    """

    def __init__(
        self,
        collectors: list[ResourceCollector],
        registry: PerformanceRegistry,
        interval_seconds: float = _DEFAULT_INTERVAL_SECONDS,
    ) -> None:
        self._collectors = collectors
        self._registry = registry
        self._interval_seconds = interval_seconds
        self._task: asyncio.Task[None] | None = None

    @property
    def is_running(self) -> bool:
        """Return whether the background sampling loop is active."""
        return self._task is not None and not self._task.done()

    def start(self) -> None:
        """Start the background sampling loop on the running event loop."""
        if self.is_running:
            return
        self._task = asyncio.ensure_future(self._run())

    async def stop(self) -> None:
        """Cancel the background sampling loop and wait for it to exit."""
        if self._task is None:
            return
        self._task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await self._task
        self._task = None

    def collect_once(self) -> list[MetricPoint]:
        """Sample every collector once, ingest the result, and return it.

        Exposed for callers (tests, a debug-dashboard "refresh now"
        action) that want an immediate synchronous sample without
        starting the periodic loop.
        """
        points = collect_all(self._collectors)
        self._registry.aggregator.ingest(points)
        return points

    async def _run(self) -> None:
        while True:
            self.collect_once()
            await asyncio.sleep(self._interval_seconds)
