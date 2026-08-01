"""Exceptions raised by the performance instrumentation subsystem."""

from __future__ import annotations

from typing import Final

_CONFIGURATION_SAMPLE_RATE_MESSAGE: Final[str] = (
    "sample_rate_percent must be between 0 and 100"
)
_CONFIGURATION_TRACE_NODES_MESSAGE: Final[str] = "max_trace_nodes must be positive"
_CONFIGURATION_HISTORY_MESSAGE: Final[str] = "max_request_history cannot be negative"
_TIMER_NOT_STARTED_MESSAGE: Final[str] = "cannot stop a timer that has not been started"
_TIMER_FINISH_BEFORE_START_MESSAGE: Final[str] = "timer finish precedes timer start"
_TIMER_CLOCK_BEFORE_START_MESSAGE: Final[str] = "timer clock moved before timer start"
_TRACE_NODE_FINISH_BEFORE_START_MESSAGE: Final[str] = (
    "trace node finish precedes node start"
)
_TRACE_CHILD_HAS_PARENT_MESSAGE: Final[str] = "trace node already has a parent"
_TRACE_ALREADY_FINISHED_MESSAGE: Final[str] = "cannot start a node on a finished trace"
_TRACE_NO_ACTIVE_PARENT_MESSAGE: Final[str] = "trace has no active parent node"
_TRACE_NO_ACTIVE_NODE_MESSAGE: Final[str] = (
    "cannot finish a trace without an active node"
)
_METRIC_TYPE_MISMATCH_MESSAGE: Final[str] = (
    "metric already registered with a different metric type"
)
_HISTOGRAM_NO_SAMPLES_MESSAGE: Final[str] = "histogram has not observed any samples"
_HISTOGRAM_INVALID_QUANTILE_MESSAGE: Final[str] = "quantile must be between 0 and 1"
_PROFILER_NOT_STARTED_MESSAGE: Final[str] = (
    "cannot open a stage before the profiler has started its root trace"
)
_PROFILER_ALREADY_COMPLETED_MESSAGE: Final[str] = (
    "cannot reuse a profiler that has already completed"
)
_PROFILER_NO_ACTIVE_STAGE_MESSAGE: Final[str] = (
    "cannot close a stage when none is active on this profiler"
)
_REGISTRY_UNKNOWN_COLLECTOR_MESSAGE: Final[str] = "no collector registered under name"


class PerformanceError(Exception):
    """Base exception for performance instrumentation failures."""


class PerformanceConfigurationError(PerformanceError):
    """Raised when performance configuration contains invalid values."""


class TraceStateError(PerformanceError):
    """Raised when a trace operation violates the trace lifecycle."""


class TimerStateError(PerformanceError):
    """Raised when a timer operation violates the timer lifecycle."""


class InvalidTimestampError(PerformanceError):
    """Raised when a finish timestamp precedes an operation start timestamp."""


class MetricStateError(PerformanceError):
    """Raised when a metric operation violates metric-type consistency."""


class HistogramError(PerformanceError):
    """Raised when a histogram is queried or configured invalidly."""


class ProfilerStateError(PerformanceError):
    """Raised when a request profiler operation violates its lifecycle."""


class RegistryError(PerformanceError):
    """Raised when the performance registry cannot satisfy a lookup."""
