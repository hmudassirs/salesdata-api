"""Request logging, distributed tracing, audit trail, and pool/app
metrics instrumentation.

Renamed from the generic-sounding pieces of core/services/service_manager.py
(LoggingService/TracingService/AuditService) to their actual roles, and
given distinct names from core.db.cache/core.db.tracing's unrelated
in-process utilities that happened to share the words "cache"/"tracing".

Also now the real home for the Prometheus counters that used to live in
core/services/observability.py. session.py's get_sync_session()/
get_async_session() has always tried `from core.observability import
POOL_REQUESTS, ...` — this package existing under that exact name, with
these names re-exported below, is what makes that import finally
resolve instead of silently hitting the bare `except Exception` fallback
it's hit until now.

Lazy __getattr__ exports, matching every other __init__.py in this
codebase, so importing this package for logging/tracing/audit doesn't
force a hard `prometheus_client` dependency just to get those.
"""

__all__ = [
    "AuditTrail",
    "RequestLogger",
    "RequestTracer",
    "build_request_context",
    "emit_request_observability",
    "POOL_REQUESTS",
    "POOL_TIMEOUTS",
    "POOL_ACTIVE",
    "POOL_WAIT",
    "APP_REQUESTS",
    "APP_ERRORS",
    "APP_LATENCY",
    "HealthStatus",
    "OpenTelemetryManager",
    "get_otel_manager",
    "traced_function",
    "trace_method_calls",
    "record_db_operation",
    "record_cache_operation",
]


def __getattr__(name: str):
    if name in {"build_request_context", "emit_request_observability"}:
        from .context import build_request_context, emit_request_observability

        mapping = {
            "build_request_context": build_request_context,
            "emit_request_observability": emit_request_observability,
        }
        return mapping[name]

    if name == "RequestLogger":
        from .request_logger import RequestLogger

        return RequestLogger

    if name == "RequestTracer":
        from .request_tracer import RequestTracer

        return RequestTracer

    if name == "AuditTrail":
        from .audit import AuditTrail

        return AuditTrail

    if name in {
        "POOL_REQUESTS",
        "POOL_TIMEOUTS",
        "POOL_ACTIVE",
        "POOL_WAIT",
        "APP_REQUESTS",
        "APP_ERRORS",
        "APP_LATENCY",
        "HealthStatus",
    }:
        from .prometheus_metrics import (
            APP_ERRORS,
            APP_LATENCY,
            APP_REQUESTS,
            POOL_ACTIVE,
            POOL_REQUESTS,
            POOL_TIMEOUTS,
            POOL_WAIT,
            HealthStatus,
        )

        mapping = {
            "POOL_REQUESTS": POOL_REQUESTS,
            "POOL_TIMEOUTS": POOL_TIMEOUTS,
            "POOL_ACTIVE": POOL_ACTIVE,
            "POOL_WAIT": POOL_WAIT,
            "APP_REQUESTS": APP_REQUESTS,
            "APP_ERRORS": APP_ERRORS,
            "APP_LATENCY": APP_LATENCY,
            "HealthStatus": HealthStatus,
        }
        return mapping[name]

    if name in {
        "OpenTelemetryManager",
        "get_otel_manager",
        "traced_function",
        "trace_method_calls",
        "record_db_operation",
        "record_cache_operation",
    }:
        from .otel import (
            OpenTelemetryManager,
            get_otel_manager,
            record_cache_operation,
            record_db_operation,
            trace_method_calls,
            traced_function,
        )

        mapping = {
            "OpenTelemetryManager": OpenTelemetryManager,
            "get_otel_manager": get_otel_manager,
            "traced_function": traced_function,
            "trace_method_calls": trace_method_calls,
            "record_db_operation": record_db_operation,
            "record_cache_operation": record_cache_operation,
        }
        return mapping[name]

    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
