"""OpenTelemetry integration for distributed tracing and metrics.

This module provides OpenTelemetry integration for the application, enabling:
- Distributed tracing across service boundaries
- Standardized metrics collection (Prometheus compatible)
- Span context propagation
- Log correlation with traces and metrics

MOVED to core/observability/otel.py. This is the *only* place in the
codebase that calls `trace.set_tracer_provider(...)` — the one call
that actually connects a tracer to a real exporter (OTLP/Prometheus).
Nothing anywhere called `get_otel_manager()` or
`OpenTelemetryManager().initialize()`, and core/db/session.py obtained
its tracer via a direct, unrelated `trace.get_tracer(__name__)` call
instead of going through this manager. The net effect: session.py's
`_TRACER.start_as_current_span(...)` calls have been creating spans
against OpenTelemetry's default no-op provider — created, but never
exported anywhere. Distributed tracing has likely never actually run.
Fixed by wiring `get_otel_manager().initialize()` into
core/app/lifespan.py's startup and having session.py obtain its tracer
through this manager instead of calling `opentelemetry.trace` directly
— see both files' diffs in MIGRATION.md.
"""

import logging
import time
from contextlib import contextmanager
from functools import wraps
from typing import Any, Callable, Optional

from opentelemetry import metrics, trace
from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
from opentelemetry.exporter.prometheus import PrometheusMetricReader
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor

logger = logging.getLogger(__name__)


class OpenTelemetryManager:
    """Manages OpenTelemetry initialization and configuration."""

    def __init__(
        self,
        service_name: str = "preparedata",
        otlp_endpoint: str = "localhost:4317",
        otlp_insecure: bool = True,
        enable_prometheus: bool = True,
        enable_otlp: bool = True,
    ):
        """Initialize OpenTelemetry manager.

        Args:
            service_name: Service name for tracing
            otlp_endpoint: OTLP gRPC collector endpoint (host:port). Matches
                the default port (4317) of the OpenTelemetry Collector and
                modern Jaeger (which accepts OTLP natively as of Jaeger 1.35+,
                so this can still point at a Jaeger instance). Can also be
                left at the default and overridden via the standard
                `OTEL_EXPORTER_OTLP_ENDPOINT` env var, which `OTLPSpanExporter`
                reads itself if `endpoint` isn't passed explicitly.
            otlp_insecure: Skip TLS for the gRPC channel (default True for
                local/sidecar collectors; set False for a collector requiring
                TLS, e.g. behind a public endpoint).
            enable_prometheus: Enable Prometheus metrics export
            enable_otlp: Enable OTLP trace export
        """
        self.service_name = service_name
        self.otlp_endpoint = otlp_endpoint
        self.otlp_insecure = otlp_insecure
        self.enable_prometheus = enable_prometheus
        self.enable_otlp = enable_otlp

        self.tracer_provider: Optional[TracerProvider] = None
        self.meter_provider: Optional[MeterProvider] = None
        self.tracer: Optional[trace.Tracer] = None
        self.meter: Optional[metrics.Meter] = None

    def initialize(self) -> None:
        """Initialize OpenTelemetry providers."""
        # Create resource
        resource = Resource.create(
            {
                "service.name": self.service_name,
                "service.version": "1.0.0",
            }
        )

        # Initialize Tracer Provider
        self.tracer_provider = TracerProvider(resource=resource)

        # Add OTLP exporter if enabled
        if self.enable_otlp:
            try:
                otlp_exporter = OTLPSpanExporter(
                    endpoint=self.otlp_endpoint,
                    insecure=self.otlp_insecure,
                )
                self.tracer_provider.add_span_processor(
                    BatchSpanProcessor(otlp_exporter)
                )
                logger.info(f"OTLP exporter enabled: {self.otlp_endpoint}")
            except Exception as e:
                logger.warning(f"Failed to initialize OTLP exporter: {e}")

        trace.set_tracer_provider(self.tracer_provider)
        self.tracer = trace.get_tracer(__name__)

        # Initialize Meter Provider
        readers = []

        # Add Prometheus reader if enabled
        if self.enable_prometheus:
            try:
                prometheus_reader = PrometheusMetricReader()
                readers.append(prometheus_reader)
                logger.info("Prometheus metrics export enabled")
            except Exception as e:
                logger.warning(f"Failed to initialize Prometheus: {e}")

        self.meter_provider = MeterProvider(resource=resource, metric_readers=readers)
        metrics.set_meter_provider(self.meter_provider)
        self.meter = metrics.get_meter(__name__)

        logger.info(f"OpenTelemetry initialized for service: {self.service_name}")

    @contextmanager
    def trace_operation(
        self,
        operation_name: str,
        attributes: Optional[dict[str, Any]] = None,
    ):
        """Context manager for tracing an operation.

        Args:
            operation_name: Name of the operation
            attributes: Optional span attributes

        Example:
            with otel_manager.trace_operation("query_database", {"table": "users"}):
                result = execute_query()
        """
        if not self.tracer:
            yield
            return

        with self.tracer.start_as_current_span(operation_name) as span:
            if attributes:
                for key, value in attributes.items():
                    span.set_attribute(key, str(value))

            try:
                yield span
            except Exception as e:
                span.set_attribute("error.type", type(e).__name__)
                span.set_attribute("error.message", str(e))
                span.set_attribute("error", True)
                raise

    def record_metric(
        self,
        metric_name: str,
        value: float,
        attributes: Optional[dict[str, str]] = None,
    ) -> None:
        """Record a metric value.

        Args:
            metric_name: Name of the metric
            value: Metric value
            attributes: Optional metric attributes
        """
        if not self.meter:
            return

        # This would typically use a counter, histogram, or gauge
        # For now, we'll log the metric
        logger.debug(f"Metric {metric_name}: {value}")

    def create_tracer(self, name: str) -> trace.Tracer:
        """Create a tracer for a module.

        Args:
            name: Module name

        Returns:
            Tracer instance
        """
        if not self.tracer_provider:
            self.initialize()

        return self.tracer_provider.get_tracer(name)


# Global instance
_otel_manager: Optional[OpenTelemetryManager] = None


def get_otel_manager() -> OpenTelemetryManager:
    """Get or create the global OpenTelemetry manager."""
    global _otel_manager
    if _otel_manager is None:
        _otel_manager = OpenTelemetryManager()
        _otel_manager.initialize()
    return _otel_manager


def traced_function(func: Callable) -> Callable:
    """Decorator to trace function execution with OpenTelemetry.

    Args:
        func: Function to trace

    Returns:
        Wrapped function with tracing

    Example:
        @traced_function
        def query_database(table_name: str):
            return execute_query(table_name)
    """

    @wraps(func)
    def wrapper(*args, **kwargs):
        manager = get_otel_manager()
        operation_name = f"{func.__module__}.{func.__name__}"

        # Prepare attributes from arguments
        attributes = {
            "function.name": func.__name__,
            "function.module": func.__module__,
        }

        # Add args/kwargs as attributes (up to reasonable limit)
        for i, arg in enumerate(args[:3]):  # Limit to first 3 args
            attributes[f"arg_{i}"] = str(arg)[:100]

        for key, value in list(kwargs.items())[:3]:  # Limit to first 3 kwargs
            attributes[f"kwarg_{key}"] = str(value)[:100]

        with manager.trace_operation(operation_name, attributes) as span:
            start_time = time.time()
            try:
                result = func(*args, **kwargs)
                elapsed = time.time() - start_time
                span.set_attribute("duration_ms", elapsed * 1000)
                span.set_attribute("status", "success")
                return result
            except Exception:
                elapsed = time.time() - start_time
                span.set_attribute("duration_ms", elapsed * 1000)
                span.set_attribute("status", "error")
                raise

    return wrapper


def trace_method_calls(cls: type) -> type:
    """Class decorator to add tracing to all public methods.

    Args:
        cls: Class to instrument

    Returns:
        Modified class with tracing

    Example:
        @trace_method_calls
        class DatabaseAdapter:
            def query(self, sql):
                return execute(sql)
    """
    manager = get_otel_manager()

    for attr_name in dir(cls):
        if not attr_name.startswith("_"):
            attr = getattr(cls, attr_name)
            if callable(attr) and not isinstance(attr, type):
                setattr(cls, attr_name, traced_function(attr))

    return cls


def record_db_operation(
    operation_type: str,
    table: str,
    rows_affected: int = 0,
    duration_ms: float = 0,
) -> None:
    """Record a database operation metric.

    Args:
        operation_type: Type of operation (SELECT, INSERT, UPDATE, DELETE)
        table: Table name
        rows_affected: Number of rows affected
        duration_ms: Operation duration in milliseconds
    """
    manager = get_otel_manager()

    attributes = {
        "db.operation": operation_type,
        "db.table": table,
        "db.rows": str(rows_affected),
        "db.duration_ms": str(duration_ms),
    }

    with manager.trace_operation(f"db.{operation_type.lower()}", attributes):
        manager.record_metric(f"db.{operation_type.lower()}.duration_ms", duration_ms)


def record_cache_operation(
    cache_type: str,
    operation: str,
    hit: bool = False,
    duration_ms: float = 0,
) -> None:
    """Record a cache operation metric.

    Args:
        cache_type: Type of cache (LRU, TTL, Hybrid)
        operation: Type of operation (get, put, evict)
        hit: Whether it was a cache hit
        duration_ms: Operation duration in milliseconds
    """
    manager = get_otel_manager()

    attributes = {
        "cache.type": cache_type,
        "cache.operation": operation,
        "cache.hit": str(hit),
        "cache.duration_ms": str(duration_ms),
    }

    with manager.trace_operation(f"cache.{operation}", attributes):
        manager.record_metric(f"cache.{operation}.duration_ms", duration_ms)


# Convenience exports
__all__ = [
    "OpenTelemetryManager",
    "get_otel_manager",
    "traced_function",
    "trace_method_calls",
    "record_db_operation",
    "record_cache_operation",
]
