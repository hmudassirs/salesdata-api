"""Framework-independent, monotonic nanosecond timer."""

from __future__ import annotations

from dataclasses import dataclass, field
from types import TracebackType
from typing import Literal, Self

from .clock import PerformanceClock
from .enums import PerformanceStage
from .exceptions import (
    _TIMER_CLOCK_BEFORE_START_MESSAGE,
    _TIMER_FINISH_BEFORE_START_MESSAGE,
    _TIMER_NOT_STARTED_MESSAGE,
    InvalidTimestampError,
    TimerStateError,
)
from .types import DurationNS, MetricName, Tags, TimestampNS


@dataclass(slots=True)
class PerformanceTimer:
    """Measure one named operation without exporting or aggregating its result."""

    name: MetricName
    stage: PerformanceStage = PerformanceStage.CUSTOM
    tags: Tags = field(default_factory=dict)
    started_ns: TimestampNS | None = field(default=None, init=False)
    finished_ns: TimestampNS | None = field(default=None, init=False)
    duration_ns: DurationNS | None = field(default=None, init=False)

    def start(self) -> Self:
        """Start a new measurement and discard any previous measurement."""
        self.started_ns = PerformanceClock.now_ns()
        self.finished_ns = None
        self.duration_ns = None
        return self

    def stop(self) -> DurationNS:
        """Finish the measurement and return its integer-nanosecond duration."""
        if self.started_ns is None:
            raise TimerStateError(_TIMER_NOT_STARTED_MESSAGE)
        if self.duration_ns is not None:
            return self.duration_ns

        finished_ns = PerformanceClock.now_ns()
        if finished_ns < self.started_ns:
            raise InvalidTimestampError(_TIMER_FINISH_BEFORE_START_MESSAGE)

        self.finished_ns = finished_ns
        self.duration_ns = DurationNS(finished_ns - self.started_ns)
        return self.duration_ns

    @property
    def is_running(self) -> bool:
        """Return whether the timer has started but not yet finished."""
        return self.started_ns is not None and self.finished_ns is None

    @property
    def elapsed_ns(self) -> DurationNS | None:
        """Return completed duration or the current elapsed nanoseconds."""
        if self.started_ns is None:
            return None
        if self.duration_ns is not None:
            return self.duration_ns

        current_ns = PerformanceClock.now_ns()
        if current_ns < self.started_ns:
            raise InvalidTimestampError(_TIMER_CLOCK_BEFORE_START_MESSAGE)
        return DurationNS(current_ns - self.started_ns)

    def __enter__(self) -> Self:
        """Start the timer for synchronous context-manager usage."""
        return self.start()

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> Literal[False]:
        """Finish the timer and propagate any exception from the body."""
        self.stop()
        return False

    async def __aenter__(self) -> Self:
        """Start the timer for asynchronous context-manager usage."""
        return self.start()

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> Literal[False]:
        """Finish the timer and propagate any exception from the body."""
        self.stop()
        return False
