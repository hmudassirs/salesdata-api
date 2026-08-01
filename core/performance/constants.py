"""Constants shared by the performance foundation."""

from __future__ import annotations

from typing import Final

NANOSECONDS_PER_MICROSECOND: Final[int] = 1_000
NANOSECONDS_PER_MILLISECOND: Final[int] = 1_000_000
NANOSECONDS_PER_SECOND: Final[int] = 1_000_000_000

PERCENT_MINIMUM: Final[int] = 0
PERCENT_MAXIMUM: Final[int] = 100
DEFAULT_SAMPLE_RATE_PERCENT: Final[int] = 100
DEFAULT_MAX_TRACE_NODES: Final[int] = 256
DEFAULT_MAX_REQUEST_HISTORY: Final[int] = 1_000

ENV_ENABLED: Final[str] = "PERF_ENABLED"
ENV_SAMPLE_RATE_PERCENT: Final[str] = "PERF_SAMPLE_RATE_PERCENT"
ENV_MAX_TRACE_NODES: Final[str] = "PERF_MAX_TRACE_NODES"
ENV_MAX_REQUEST_HISTORY: Final[str] = "PERF_MAX_REQUEST_HISTORY"

# Default duration-histogram bucket upper bounds, in whole milliseconds.
# Chosen to give useful resolution across the sub-millisecond to
# multi-second range typical of database-backed HTTP request latency.
DEFAULT_HISTOGRAM_BUCKET_BOUNDS_MS: Final[tuple[float, ...]] = (
    0.5,
    1,
    2,
    5,
    10,
    25,
    50,
    100,
    250,
    500,
    1_000,
    2_500,
    5_000,
    10_000,
)

# Quantiles the streaming P^2 estimator tracks by default.
DEFAULT_STREAMING_QUANTILES: Final[tuple[float, ...]] = (0.5, 0.9, 0.95, 0.99)

DEFAULT_REGISTRY_NAME: Final[str] = "default"
