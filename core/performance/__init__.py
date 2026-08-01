"""Framework-independent, high-resolution performance primitives.

Import surface for `core.performance`. The middleware, adapters,
collectors, exporters, and dashboard sub-packages are intentionally not
re-exported here: they carry optional dependencies (FastAPI, specific
subsystems) that the foundation and core layers must not require just to
be imported. Import those directly, e.g.
`from core.performance.middleware.fastapi import install_performance_middleware`.
"""

from .aggregator import MetricsAggregator
from .clock import PerformanceClock
from .config import PerformanceConfig
from .context import (
    bind_profiler,
    get_current_profiler,
    reset_current_profiler,
    set_current_profiler,
)
from .enums import MetricType, MetricUnit, PerformanceStage
from .exceptions import (
    HistogramError,
    InvalidTimestampError,
    MetricStateError,
    PerformanceConfigurationError,
    PerformanceError,
    ProfilerStateError,
    RegistryError,
    TimerStateError,
    TraceStateError,
)
from .histogram import Histogram, StreamingHistogram, StreamingPercentileEstimator
from .metric import Counter, Gauge, MetricPoint
from .registry import (
    PerformanceRegistry,
    get_default_registry,
    set_default_registry,
)
from .request_profiler import (
    NullRequestProfiler,
    RequestProfile,
    RequestProfiler,
)
from .timer import PerformanceTimer
from .trace import Trace, TraceNode
from .types import (
    ConnectionId,
    DurationNS,
    Metadata,
    MetricName,
    MetricTag,
    MetricValue,
    QueryId,
    RequestId,
    Tags,
    TimestampNS,
    TraceId,
)

__all__ = [
    "ConnectionId",
    "Counter",
    "DurationNS",
    "Gauge",
    "Histogram",
    "HistogramError",
    "InvalidTimestampError",
    "Metadata",
    "MetricName",
    "MetricPoint",
    "MetricStateError",
    "MetricTag",
    "MetricType",
    "MetricUnit",
    "MetricValue",
    "MetricsAggregator",
    "NullRequestProfiler",
    "PerformanceClock",
    "PerformanceConfig",
    "PerformanceConfigurationError",
    "PerformanceError",
    "PerformanceRegistry",
    "PerformanceStage",
    "PerformanceTimer",
    "ProfilerStateError",
    "QueryId",
    "RegistryError",
    "RequestId",
    "RequestProfile",
    "RequestProfiler",
    "StreamingHistogram",
    "StreamingPercentileEstimator",
    "Tags",
    "TimerStateError",
    "TimestampNS",
    "Trace",
    "TraceId",
    "TraceNode",
    "TraceStateError",
    "bind_profiler",
    "get_current_profiler",
    "get_default_registry",
    "reset_current_profiler",
    "set_current_profiler",
    "set_default_registry",
]
