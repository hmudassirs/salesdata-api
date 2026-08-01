"""Request-scoped profiler: nested stage timing plus counters/gauges/events.

`RequestProfiler` is the main object application code (or, later, the
FastAPI middleware) reaches for. It owns one `Trace`, is safe to use in
plain scripts, background workers, or tests with no web framework
involved, and produces one immutable `RequestProfile` when `complete()`
is called.

Two implementations share the same public surface:

- `RequestProfiler` does real work: it starts/finishes trace nodes and
  appends `MetricPoint`s.
- `NullRequestProfiler` is the disabled path. Every method is a no-op
  that returns cheaply, so code can call `profiler.stage(...)`
  unconditionally without an `if enabled:` check at every call site, and
  the disabled path still does zero clock reads, allocation, or trace
  work, per `docs/Architecture.md`'s compatibility constraints.
"""

from __future__ import annotations

from contextlib import AbstractAsyncContextManager, AbstractContextManager
from dataclasses import dataclass, field
from types import TracebackType
from typing import Literal, Self
from uuid import uuid4

from .clock import PerformanceClock
from .enums import MetricType, MetricUnit, PerformanceStage
from .exceptions import (
    _PROFILER_ALREADY_COMPLETED_MESSAGE,
    _PROFILER_NOT_STARTED_MESSAGE,
    ProfilerStateError,
)
from .metric import MetricPoint
from .trace import Trace, TraceNode
from .types import (
    DurationNS,
    Metadata,
    MetricName,
    MetricValue,
    RequestId,
    Tags,
    TimestampNS,
)

STATUS_OK = "ok"
STATUS_ERROR = "error"


@dataclass(frozen=True, slots=True)
class RequestProfile:
    """Immutable result of one completed profiler run.

    This is the object handed to `PerformanceRegistry.record_completed_request`
    and, from it, to collectors and exporters. Nothing about it is
    mutated after `RequestProfiler.complete()` returns it.
    """

    request_id: RequestId
    trace: Trace | None
    metric_points: list[MetricPoint]
    tags: Tags
    metadata: Metadata
    started_ns: TimestampNS
    finished_ns: TimestampNS
    duration_ns: DurationNS
    status: str = STATUS_OK
    error: str | None = None

    def to_dict(self) -> dict[str, object]:
        """Render a JSON-friendly nested dict, e.g. for the dashboard/exporters."""
        return {
            "request_id": self.request_id,
            "status": self.status,
            "error": self.error,
            "started_ns": self.started_ns,
            "finished_ns": self.finished_ns,
            "duration_ns": self.duration_ns,
            "tags": dict(self.tags),
            "metadata": dict(self.metadata),
            "trace": _node_to_dict(self.trace.root) if self.trace else None,
            "metrics": [
                {
                    "name": point.name,
                    "type": point.metric_type.name,
                    "value": point.value,
                    "unit": point.unit.value,
                    "tags": dict(point.tags),
                }
                for point in self.metric_points
            ],
        }


def _node_to_dict(node: TraceNode | None) -> dict[str, object] | None:
    if node is None:
        return None
    return {
        "name": node.name,
        "stage": node.stage.name,
        "started_ns": node.started_ns,
        "finished_ns": node.finished_ns,
        "duration_ns": node.duration_ns,
        "exclusive_duration_ns": node.exclusive_duration_ns,
        "tags": dict(node.tags),
        "metadata": dict(node.metadata),
        "children": [_node_to_dict(child) for child in node.children],
    }


class _StageScope(
    AbstractContextManager["TraceNode"], AbstractAsyncContextManager["TraceNode"]
):
    """Context manager returned by `RequestProfiler.stage(...)`.

    Supports both `with` and `async with` so the same call site works in
    sync adapters (pool checkout) and async endpoints/dependencies alike.
    """

    __slots__ = ("_metadata", "_name", "_node", "_profiler", "_stage", "_tags")

    def __init__(
        self,
        profiler: RequestProfiler,
        stage: PerformanceStage,
        name: MetricName,
        tags: Tags | None,
        metadata: Metadata | None,
    ) -> None:
        self._profiler = profiler
        self._stage = stage
        self._name = name
        self._tags = tags
        self._metadata = metadata
        self._node: TraceNode | None = None

    def __enter__(self) -> TraceNode:
        self._node = self._profiler._start_stage(  # noqa: SLF001
            self._stage, self._name, self._tags, self._metadata
        )
        return self._node

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> Literal[False]:
        self._profiler._finish_stage()  # noqa: SLF001
        return False

    async def __aenter__(self) -> TraceNode:
        return self.__enter__()

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> Literal[False]:
        return self.__exit__(exc_type, exc_value, traceback)


@dataclass(slots=True)
class RequestProfiler:
    """Owns one request-local `Trace` plus emitted counters/gauges/events."""

    request_id: RequestId = field(default_factory=lambda: RequestId(uuid4().hex))
    tags: Tags = field(default_factory=dict)
    metadata: Metadata = field(default_factory=dict)
    trace: Trace = field(init=False)
    metric_points: list[MetricPoint] = field(default_factory=list, init=False)
    started_ns: TimestampNS = field(init=False)
    _completed: bool = field(default=False, init=False, repr=False)

    def __post_init__(self) -> None:
        """Start the clock and open the root REQUEST trace node."""
        self.trace = Trace(tags=dict(self.tags), metadata=dict(self.metadata))
        self.started_ns = PerformanceClock.now_ns()
        self.trace.start(PerformanceStage.REQUEST, MetricName("request"))

    def stage(
        self,
        stage: PerformanceStage,
        name: MetricName,
        *,
        tags: Tags | None = None,
        metadata: Metadata | None = None,
    ) -> _StageScope:
        """Open a nested stage under the currently active node.

        Usable as `with profiler.stage(...):` or `async with profiler.stage(...):`.
        """
        return _StageScope(self, stage, name, tags, metadata)

    def _start_stage(
        self,
        stage: PerformanceStage,
        name: MetricName,
        tags: Tags | None,
        metadata: Metadata | None,
    ) -> TraceNode:
        if self._completed:
            raise ProfilerStateError(_PROFILER_ALREADY_COMPLETED_MESSAGE)
        if self.trace.current is None:
            raise ProfilerStateError(_PROFILER_NOT_STARTED_MESSAGE)
        return self.trace.start(stage, name, tags=tags, metadata=metadata)

    def _finish_stage(self) -> None:
        node = self.trace.current
        duration_ns = self.trace.finish()
        if node is not None:
            self.metric_points.append(
                MetricPoint(
                    name=node.name,
                    metric_type=MetricType.TIMER,
                    value=duration_ns,
                    timestamp_ns=PerformanceClock.now_ns(),
                    unit=MetricUnit.NANOSECONDS,
                    tags=dict(node.tags),
                )
            )

    def counter(
        self, name: MetricName, amount: MetricValue = 1, *, tags: Tags | None = None
    ) -> None:
        """Emit a counter increment tied to this request."""
        self._emit(name, MetricType.COUNTER, amount, MetricUnit.COUNT, tags)

    def gauge(
        self, name: MetricName, value: MetricValue, *, tags: Tags | None = None
    ) -> None:
        """Emit a point-in-time gauge reading tied to this request."""
        self._emit(name, MetricType.GAUGE, value, MetricUnit.COUNT, tags)

    def event(
        self,
        name: MetricName,
        *,
        tags: Tags | None = None,
        metadata: Metadata | None = None,
    ) -> None:
        """Record a discrete, non-numeric event tied to this request."""
        point = MetricPoint(
            name=name,
            metric_type=MetricType.EVENT,
            value=1,
            timestamp_ns=PerformanceClock.now_ns(),
            unit=MetricUnit.COUNT,
            tags={} if tags is None else tags,
            metadata={} if metadata is None else metadata,
        )
        self.metric_points.append(point)

    def _emit(
        self,
        name: MetricName,
        metric_type: MetricType,
        value: MetricValue,
        unit: MetricUnit,
        tags: Tags | None,
    ) -> None:
        if self._completed:
            raise ProfilerStateError(_PROFILER_ALREADY_COMPLETED_MESSAGE)
        self.metric_points.append(
            MetricPoint(
                name=name,
                metric_type=metric_type,
                value=value,
                timestamp_ns=PerformanceClock.now_ns(),
                unit=unit,
                tags={} if tags is None else tags,
            )
        )

    def complete(
        self, status: str = STATUS_OK, error: str | None = None
    ) -> RequestProfile:
        """Finish any still-open stages and return the immutable result.

        Idempotent-by-construction is not required here: a profiler is
        meant to be completed exactly once, at the natural end of a
        request. Calling it twice raises, since a second call would
        silently produce a second, differently timed profile.
        """
        if self._completed:
            raise ProfilerStateError(_PROFILER_ALREADY_COMPLETED_MESSAGE)
        self.trace.finish_all()
        finished_ns = PerformanceClock.now_ns()
        self._completed = True
        return RequestProfile(
            request_id=self.request_id,
            trace=self.trace,
            metric_points=list(self.metric_points),
            tags=dict(self.tags),
            metadata=dict(self.metadata),
            started_ns=self.started_ns,
            finished_ns=finished_ns,
            duration_ns=DurationNS(finished_ns - self.started_ns),
            status=status,
            error=error,
        )

    def __enter__(self) -> Self:
        """Allow `with RequestProfiler() as profiler:` for script/test usage."""
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> Literal[False]:
        """Complete the profiler, recording failure status on exception."""
        if not self._completed:
            if exc_value is None:
                self.complete()
            else:
                self.complete(status=STATUS_ERROR, error=str(exc_value))
        return False


class NullRequestProfiler:
    """Disabled-path profiler: every operation is a cheap no-op.

    Deliberately does not subclass `RequestProfiler` or a shared ABC —
    that would cost a clock read and dataclass field allocation in
    `__post_init__` just to be immediately discarded. This class holds
    no state at all.
    """

    __slots__ = ()

    def stage(
        self,
        stage: PerformanceStage,  # noqa: ARG002
        name: MetricName,  # noqa: ARG002
        *,
        tags: Tags | None = None,  # noqa: ARG002
        metadata: Metadata | None = None,  # noqa: ARG002
    ) -> _NullStageScope:
        """Return a no-op stage scope; nothing is timed or recorded."""
        return _NULL_STAGE_SCOPE

    def counter(
        self, name: MetricName, amount: MetricValue = 1, *, tags: Tags | None = None
    ) -> None:
        """No-op: the disabled path never allocates a metric point."""

    def gauge(
        self, name: MetricName, value: MetricValue, *, tags: Tags | None = None
    ) -> None:
        """No-op: the disabled path never allocates a metric point."""

    def event(
        self,
        name: MetricName,
        *,
        tags: Tags | None = None,
        metadata: Metadata | None = None,
    ) -> None:
        """No-op: the disabled path never allocates a metric point."""

    def complete(self, status: str = STATUS_OK, error: str | None = None) -> None:
        """No-op: there is nothing to finish or return."""

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> Literal[False]:
        return False


class _NullStageScope:
    """No-op stand-in for `_StageScope` used by `NullRequestProfiler`."""

    __slots__ = ()

    def __enter__(self) -> None:
        return None

    def __exit__(self, *exc_info: object) -> Literal[False]:
        return False

    async def __aenter__(self) -> None:
        return None

    async def __aexit__(self, *exc_info: object) -> Literal[False]:
        return False


_NULL_STAGE_SCOPE = _NullStageScope()

AnyProfiler = RequestProfiler | NullRequestProfiler
