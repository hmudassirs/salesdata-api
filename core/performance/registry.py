"""Central, in-process owner of aggregation, collectors, and exporters.

`PerformanceRegistry` is where the "Trace/Event -> Collector -> Registry
-> Aggregator -> Exporter" pipeline from `docs/PerformancePlan.md` is
wired together. It is intentionally the only stateful, shared object in
the subsystem: everything upstream of it (`PerformanceTimer`, `Trace`,
`RequestProfiler`) is request-local and lock-free, and everything
downstream (`Collector`, `Exporter`) is optional and pulls from it rather
than being pushed to synchronously on the request path.

`record_completed_request` is the one method called from (the end of) a
request. It only appends to a bounded, GIL-protected `deque` and folds
already-computed points into the aggregator's in-memory dicts — no I/O,
no exporter call, no lock. Collectors and exporters read from the
registry later, out of band.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

from .aggregator import MetricsAggregator
from .constants import DEFAULT_MAX_REQUEST_HISTORY
from .exceptions import RegistryError
from .histogram import StreamingHistogram
from .metric import Counter, Gauge
from .request_profiler import RequestProfile


@runtime_checkable
class Collector(Protocol):
    """Something that can observe a just-completed request profile.

    Collectors are called synchronously by the registry, so they must be
    cheap and non-blocking (e.g. appending to an in-memory structure).
    Anything that does I/O belongs in an `Exporter`, run out of band.
    """

    name: str

    def collect(self, profile: RequestProfile) -> None:
        """Observe one completed `RequestProfile`."""


@runtime_checkable
class Exporter(Protocol):
    """Something that converts current registry state to an external form."""

    name: str

    def export(self, registry: PerformanceRegistry) -> None:
        """Read current registry state and publish it externally."""


@dataclass(slots=True)
class PerformanceRegistry:
    """Own the aggregator, completed-request history, collectors, exporters."""

    max_request_history: int = DEFAULT_MAX_REQUEST_HISTORY
    aggregator: MetricsAggregator = field(default_factory=MetricsAggregator)
    _history: deque[RequestProfile] = field(init=False, repr=False)
    _history_by_id: dict[str, RequestProfile] = field(
        default_factory=dict, init=False, repr=False
    )
    _collectors: dict[str, Collector] = field(default_factory=dict, repr=False)
    _exporters: dict[str, Exporter] = field(default_factory=dict, repr=False)
    _total_requests_recorded: int = field(default=0, init=False, repr=False)

    def __post_init__(self) -> None:
        """Allocate the bounded completed-request ring buffer."""
        self._history = deque(maxlen=self.max_request_history or None)

    def record_completed_request(self, profile: RequestProfile) -> None:
        """Ingest one completed request: fold points, retain history, notify.

        Called once, at request end. Cheap by construction: dict/deque
        mutation and simple arithmetic only, never I/O — including
        eviction once the bounded history is at capacity. `self._history`
        is a `deque(maxlen=...)`, which always evicts exactly the single
        oldest entry on the next `append` once full; that entry's id is
        read *before* appending and removed directly from
        `_history_by_id`, an O(1) lookup+delete, rather than rebuilding
        the full retained-id set every call to find it (see
        `docs/performance/README.md`'s Phase 14 before/after report for
        why this matters under sustained load).
        """
        self.aggregator.ingest(profile.metric_points)
        self._total_requests_recorded += 1
        evicted_id: str | None = None
        if (
            self.max_request_history
            and len(self._history) >= self.max_request_history
        ):
            evicted_id = self._history[0].request_id
        self._history.append(profile)
        self._history_by_id[profile.request_id] = profile
        if evicted_id is not None and evicted_id != profile.request_id:
            del self._history_by_id[evicted_id]
        for collector in self._collectors.values():
            collector.collect(profile)

    def register_collector(self, collector: Collector) -> None:
        """Register a collector under its `name`, replacing any prior one."""
        self._collectors[collector.name] = collector

    def register_exporter(self, exporter: Exporter) -> None:
        """Register an exporter under its `name`, replacing any prior one."""
        self._exporters[exporter.name] = exporter

    def unregister_collector(self, name: str) -> None:
        """Remove a previously registered collector, if present."""
        self._collectors.pop(name, None)

    def unregister_exporter(self, name: str) -> None:
        """Remove a previously registered exporter, if present."""
        self._exporters.pop(name, None)

    def export(self, name: str) -> None:
        """Run one registered exporter by name."""
        exporter = self._exporters.get(name)
        if exporter is None:
            raise RegistryError(f"no exporter registered as {name!r}")  # noqa: TRY003
        exporter.export(self)

    def export_all(self) -> None:
        """Run every registered exporter."""
        for exporter in self._exporters.values():
            exporter.export(self)

    def history(self) -> list[RequestProfile]:
        """Return completed request profiles, oldest first."""
        return list(self._history)

    @property
    def total_requests_recorded(self) -> int:
        """Total requests ever recorded, unaffected by history eviction.

        Unlike `len(history())` (bounded by `max_request_history`, and
        constant once history is at capacity), this only ever grows —
        useful for computing throughput (delta of two readings over the
        elapsed time between them) the way the live dashboard does.
        """
        return self._total_requests_recorded

    def get_request(self, request_id: str) -> RequestProfile | None:
        """Look up one retained completed request profile by id."""
        return self._history_by_id.get(request_id)

    def counters(self) -> list[Counter]:
        """Return the aggregator's current counters."""
        return self.aggregator.counters()

    def gauges(self) -> list[Gauge]:
        """Return the aggregator's current gauges."""
        return self.aggregator.gauges()

    def histograms(self) -> list[StreamingHistogram]:
        """Return the aggregator's current histograms."""
        return self.aggregator.histograms()

    def reset(self) -> None:
        """Clear history and aggregate state; registered collectors/exporters remain."""
        self._history.clear()
        self._history_by_id.clear()
        self._total_requests_recorded = 0
        self.aggregator.reset()


_default_registry: PerformanceRegistry | None = None


def get_default_registry() -> PerformanceRegistry:
    """Return the process-wide default registry, creating it on first use."""
    global _default_registry  # noqa: PLW0603
    if _default_registry is None:
        _default_registry = PerformanceRegistry()
    return _default_registry


def set_default_registry(registry: PerformanceRegistry) -> None:
    """Replace the process-wide default registry, e.g. in tests."""
    global _default_registry  # noqa: PLW0603
    _default_registry = registry
