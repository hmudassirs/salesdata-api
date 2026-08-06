"""FastAPI application factory and configuration.

This file is now just a factory: CORS, OpenAPI schema, router mounting,
and installing the two middlewares. The actual auth-check and
observability logic each moved to live with their own domain — see
core.auth.middleware and core.observability.middleware.
"""

from typing import Optional

from fastapi import Depends, FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.openapi.utils import get_openapi
from fastapi.security import APIKeyHeader
from fastapi.responses import ORJSONResponse

from core.app.api.routes import auth_router, router
from core.app.settings import AppSettings
from core.auth.dependencies import require_admin_user
from core.auth.middleware import install_auth_middleware
from core.db.session import DatabaseSession
from core.observability.middleware import install_observability_middleware
from core.performance.config import PerformanceConfig
from core.performance.dashboard import install_performance_dashboard
from core.performance.middleware.fastapi import install_performance_middleware

api_key_header = APIKeyHeader(name="x-api-key", auto_error=False)


def custom_openapi_schema(app: FastAPI):
    """Expose API-key auth in the generated OpenAPI schema for Swagger UI."""
    if app.openapi_schema:
        return app.openapi_schema

    openapi_schema = get_openapi(
        title=app.title,
        version=app.version,
        openapi_version=app.openapi_version,
        description=app.description,
        routes=app.routes,
    )
    openapi_schema.setdefault("components", {}).setdefault("securitySchemes", {})[
        "ApiKeyAuth"
    ] = {
        "type": "apiKey",
        "in": "header",
        "name": "x-api-key",
        "description": "Enter your API key in the x-api-key header",
    }

    for path_item in openapi_schema.get("paths", {}).values():
        for operation in path_item.values():
            if isinstance(operation, dict):
                operation.setdefault("security", [{"ApiKeyAuth": []}])

    app.openapi_schema = openapi_schema
    return app.openapi_schema


def create_app(
    db_session: Optional[DatabaseSession] = None,
    settings: Optional[AppSettings] = None,
    title: str = "PrepareData API",
    version: str = "1.0.0",
    description: str = "Database preparation and management API",
) -> FastAPI:
    """Create and configure FastAPI application.

    Args:
        db_session: Optional DatabaseSession instance for dependency injection
        settings: AppSettings instance (JWT secret, CORS config, etc). If not
            provided, falls back to AppSettings.from_env().
        title: API title
        version: API version
        description: API description

    Returns:
        Configured FastAPI application
    """
    settings = settings or AppSettings.from_env()

    # Size the Phase-14 concurrency semaphores and Phase-13.4 result/
    # duration limits from settings, same pattern as run_api.py's
    # configure_executors() call -- one process-wide configuration
    # point, called once, before any request can reach QueryService.
    from core.services.query_limits import configure_query_limits

    configure_query_limits(
        max_result_rows=settings.max_result_rows,
        max_result_bytes=settings.max_result_bytes,
        max_query_duration_seconds=settings.max_query_duration_seconds,
        fast_concurrency=settings.fast_query_concurrency_limit,
        normal_concurrency=settings.normal_query_concurrency_limit,
        expensive_concurrency=settings.expensive_query_concurrency_limit,
    )

    app = FastAPI(
        default_response_class=ORJSONResponse,
        title=title,
        version=version,
        description=description,
        docs_url="/docs",
        redoc_url="/redoc",
    )
    app.swagger_ui_parameters = {"persistAuthorization": True}
    app.openapi = lambda: custom_openapi_schema(app)

    # Instrument with OpenTelemetry's FastAPI integration: gives every
    # incoming request a real root span (named "METHOD /path"), with the
    # existing db.acquire/db.execute/etc. spans nesting under it as
    # children. Without this, every trace's root span is whichever
    # internal db.* span happened to run — which is why Jaeger's trace
    # list showed the same one or two captions no matter which route was
    # actually hit; there was no HTTP-level span to distinguish them.
    # Optional dependency, matching the rest of this codebase's pattern
    # for OTel integrations: absence must not crash the app.
    try:
        from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor

        FastAPIInstrumentor.instrument_app(app)
    except Exception:
        import logging

        logging.getLogger(__name__).warning(
            "opentelemetry-instrumentation-fastapi not installed or failed to "
            "initialize; requests will not get a root trace span. "
            "pip install opentelemetry-instrumentation-fastapi to enable.",
            exc_info=True,
        )

    # Store settings for use in routes/dependencies (e.g. JWT secret).
    app.state.settings = settings

    # Add CORS middleware, driven by settings rather than a hardcoded
    # wildcard-origin + credentials combination (insecure, and rejected
    # by browsers in practice).
    app.add_middleware(
        CORSMiddleware,
        allow_origins=list(settings.cors_allow_origins),
        allow_credentials=settings.cors_allow_credentials,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # IMPORTANT ordering note: in Starlette, the *last* middleware
    # registered via @app.middleware("http") ends up outermost (runs
    # first on the way in). The original single combined middleware
    # ran the auth check first, and — critically — returned its 401
    # immediately, *before* ever touching the observability try/except,
    # so auth failures were never logged/traced/audited. To preserve
    # that exact behavior with two separate middlewares, auth must be
    # outermost, so it's registered second/last here. If you see auth
    # failures showing up in request logs that didn't before, this
    # ordering assumption is the first thing to check.
    install_observability_middleware(app)
    install_auth_middleware(app)
    # Installed after auth (Starlette runs the last-registered middleware
    # first/outermost), so profiling covers the whole request including
    # rejected/unauthenticated ones — see install_performance_middleware's
    # own docstring. Disabled/a no-op end to end unless PERF_ENABLED=1 (or
    # an explicit PerformanceConfig(enabled=True) is passed to create_app);
    # see docs/performance/configuration.md.
    install_performance_middleware(app, config=PerformanceConfig.from_env())

    # Include routers
    app.include_router(router)
    app.include_router(auth_router)

    # Read-only live performance dashboard (throughput/latency, pool
    # contention, SQL timing, CPU/memory, request traces — see
    # docs/performance/collectors-exporters-dashboard.md). Admin-gated:
    # unlike /api/*, /debug/* isn't covered by core.auth.middleware's
    # path-prefix check, so this dashboard would otherwise be reachable
    # unauthenticated.
    install_performance_dashboard(app, dependencies=[Depends(require_admin_user)])

    # Store db_session for dependency injection
    if db_session:
        app.state.db_session = db_session

    return app
