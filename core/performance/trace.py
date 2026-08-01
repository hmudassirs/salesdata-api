"""Request-local hierarchical tracing primitives."""

from __future__ import annotations

from dataclasses import dataclass, field
from uuid import uuid4

from .clock import PerformanceClock
from .enums import PerformanceStage
from .exceptions import (
    _TRACE_ALREADY_FINISHED_MESSAGE,
    _TRACE_CHILD_HAS_PARENT_MESSAGE,
    _TRACE_NO_ACTIVE_NODE_MESSAGE,
    _TRACE_NO_ACTIVE_PARENT_MESSAGE,
    _TRACE_NODE_FINISH_BEFORE_START_MESSAGE,
    InvalidTimestampError,
    TraceStateError,
)
from .types import DurationNS, Metadata, MetricName, Tags, TimestampNS, TraceId


@dataclass(slots=True)
class TraceNode:
    """One timed node in a request-local trace tree."""

    name: MetricName
    stage: PerformanceStage
    started_ns: TimestampNS
    tags: Tags = field(default_factory=dict)
    metadata: Metadata = field(default_factory=dict)
    finished_ns: TimestampNS | None = None
    duration_ns: DurationNS | None = None
    parent: TraceNode | None = field(default=None, repr=False)
    children: list[TraceNode] = field(default_factory=list)

    def finish(self) -> DurationNS:
        """Finish the node idempotently and return its duration."""
        if self.duration_ns is not None:
            return self.duration_ns

        finished_ns = PerformanceClock.now_ns()
        if finished_ns < self.started_ns:
            raise InvalidTimestampError(_TRACE_NODE_FINISH_BEFORE_START_MESSAGE)

        self.finished_ns = finished_ns
        self.duration_ns = DurationNS(finished_ns - self.started_ns)
        return self.duration_ns

    def add_child(self, child: TraceNode) -> None:
        """Attach an unparented child node to this node."""
        if child.parent is not None:
            raise TraceStateError(_TRACE_CHILD_HAS_PARENT_MESSAGE)
        self.children.append(child)
        child.parent = self

    @property
    def is_finished(self) -> bool:
        """Return whether the node has a completed duration."""
        return self.duration_ns is not None

    @property
    def inclusive_duration_ns(self) -> DurationNS | None:
        """Return total node duration, including time spent in children."""
        return self.duration_ns

    @property
    def exclusive_duration_ns(self) -> DurationNS | None:
        """Return node duration excluding completed child durations."""
        if self.duration_ns is None:
            return None
        child_duration = sum((child.duration_ns or 0) for child in self.children)
        return DurationNS(max(0, self.duration_ns - child_duration))


@dataclass(slots=True)
class Trace:
    """Maintain the nested node tree for one sampled operation."""

    trace_id: TraceId = field(default_factory=lambda: TraceId(uuid4().hex))
    tags: Tags = field(default_factory=dict)
    metadata: Metadata = field(default_factory=dict)
    root: TraceNode | None = field(default=None, init=False)
    current: TraceNode | None = field(default=None, init=False)

    def start(
        self,
        stage: PerformanceStage,
        name: MetricName,
        *,
        tags: Tags | None = None,
        metadata: Metadata | None = None,
    ) -> TraceNode:
        """Start a node as the root or as a child of the active node."""
        if self.root is not None and self.current is None:
            raise TraceStateError(_TRACE_ALREADY_FINISHED_MESSAGE)

        node = TraceNode(
            name=name,
            stage=stage,
            started_ns=PerformanceClock.now_ns(),
            tags={} if tags is None else tags,
            metadata={} if metadata is None else metadata,
        )
        if self.root is None:
            self.root = node
        else:
            if self.current is None:
                raise TraceStateError(_TRACE_NO_ACTIVE_PARENT_MESSAGE)
            self.current.add_child(node)
        self.current = node
        return node

    def finish(self) -> DurationNS:
        """Finish the active node and resume its parent, if any."""
        if self.current is None:
            raise TraceStateError(_TRACE_NO_ACTIVE_NODE_MESSAGE)

        node = self.current
        duration_ns = node.finish()
        self.current = node.parent
        return duration_ns

    def finish_all(self) -> None:
        """Finish the active node and every unfinished ancestor."""
        while self.current is not None:
            self.finish()

    @property
    def is_finished(self) -> bool:
        """Return whether a root node exists and every active node is closed."""
        return self.root is not None and self.current is None
