"""Prometheus metric definitions for pool and application instrumentation.

Moved from core/services/observability.py. This is what session.py's
get_sync_session()/get_async_session() has been trying to import as
`from core.observability import POOL_REQUESTS, ...` all along — except
`core.observability` was never a real module before this restructure
(only core.app.observability and core.services.observability existed,
neither of them a bare `core.observability`). The bare `except Exception`
around that import silently swallowed the ImportError, so this
instrumentation has likely never actually recorded anything. Giving
`core.observability` a real package with these names re-exported at
the top level (see core/observability/__init__.py) is what finally
makes that import succeed.
"""

import json
import logging
from datetime import datetime
from typing import Any, Callable, Dict, Optional

from prometheus_client import Counter, Gauge, Histogram

logger = logging.getLogger(__name__)

# Prometheus metrics
POOL_REQUESTS = Counter("pool_requests_total", "Total pool requests")
POOL_TIMEOUTS = Counter("pool_timeouts_total", "Pool timeouts")
POOL_ACTIVE = Gauge("pool_active_connections", "Active connections")
POOL_WAIT = Histogram("pool_wait_seconds", "Connection wait time")

# Application metrics
APP_REQUESTS = Counter("app_requests_total", "Total application requests")
APP_ERRORS = Counter("app_errors_total", "Total application errors")
APP_LATENCY = Histogram("app_latency_seconds", "Application request latency")


class HealthStatus:
    """Health status representation."""

    HEALTHY = "healthy"
    DEGRADED = "degraded"
    UNHEALTHY = "unhealthy"
