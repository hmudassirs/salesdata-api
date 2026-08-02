"""Enumerations used by the framework-independent performance subsystem."""

from __future__ import annotations

from enum import Enum, auto


class PerformanceStage(Enum):
    """Standardized execution stages for a performance trace."""

    REQUEST = auto()
    AUTHENTICATION = auto()
    AUTHORIZATION = auto()
    API_KEY_LOOKUP = auto()
    DEPENDENCY = auto()
    CONTAINER = auto()
    CACHE_LOOKUP = auto()
    CACHE_L1_LOOKUP = auto()
    CACHE_L2_LOOKUP = auto()
    CACHE_STORE = auto()
    SINGLE_FLIGHT_WAIT = auto()
    POOL_WAIT = auto()
    POOL_ACQUIRE = auto()
    POOL_RELEASE = auto()
    DB_EXECUTOR_WAIT = auto()
    TRANSACTION_BEGIN = auto()
    TRANSACTION_COMMIT = auto()
    TRANSACTION_ROLLBACK = auto()
    SQL_PREPARE = auto()
    SQL_EXECUTE = auto()
    SQL_FETCH = auto()
    SERIALIZE = auto()
    RESPONSE = auto()
    BACKGROUND = auto()
    GC = auto()
    CUSTOM = auto()


class MetricType(Enum):
    """Supported event and aggregate metric kinds."""

    COUNTER = auto()
    GAUGE = auto()
    HISTOGRAM = auto()
    TIMER = auto()
    EVENT = auto()
    TRACE = auto()


class MetricUnit(str, Enum):
    """Units that exporters may use when presenting metric values."""

    NANOSECONDS = "ns"
    MICROSECONDS = "us"
    MILLISECONDS = "ms"
    SECONDS = "s"
    COUNT = "count"
    BYTES = "bytes"
    KILOBYTES = "KB"
    MEGABYTES = "MB"
    PERCENT = "%"
