"""FastAPI middleware that opens and closes one `RequestProfiler` per request.

This is the only FastAPI-dependent layer in `core.performance`, per
`docs/PerformancePlan.md` Phase 4. It does not know anything about
databases, pools, or authentication — it creates a profiler, attaches it
to `request.state`, records request-level metadata, completes the
profiler on every exit path (normal response, raised exception, or
client-disconnect cancellation), and hands the result to a
`PerformanceRegistry`. Adapters in `core.performance.adapters` are what
give that profiler anything more specific to time.

Per `docs/RequestFlow.md`, the existing authentication middleware in
`core.auth.middleware` is installed last (so it runs first/outermost) to
preserve current 401 behaviour. If you want rejected/unauthenticated
requests to also produce a trace, install this middleware *after*
`install_auth_middleware(app)` (Starlette runs the last-registered
middleware first), so this one ends up outside auth. Installing it
before auth instead profiles only requests that pass authentication.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable

from fastapi import FastAPI, Request, Response

from core.performance.config import PerformanceConfig
from core.performance.context import bind_profiler
from core.performance.enums import PerformanceStage
from core.performance.registry import PerformanceRegistry, get_default_registry
from core.performance.request_profiler import (
    STATUS_ERROR,
    STATUS_OK,
    NullRequestProfiler,
    RequestProfiler,
)
from core.performance.types import MetricName

_STATE_ATTRIBUTE = "performance_profiler"

DispatchCallable = Callable[[Request], Awaitable[Response]]


def install_performance_middleware(
    app: FastAPI,
    config: PerformanceConfig | None = None,
    registry: PerformanceRegistry | None = None,
) -> None:
    """Register the profiling middleware on `app`.

    `config` defaults to `PerformanceConfig.from_env()` and `registry` to
    the process-wide default registry, matching every other optional
    subsystem in this codebase (e.g. Prometheus/OpenTelemetry in
    `core.app.api.app`): absence of explicit configuration degrades to a
    safe, disabled-by-default state rather than raising.
    """
    resolved_config = config or PerformanceConfig.from_env()
    resolved_registry = registry or get_default_registry()

    @app.middleware("http")
    async def profile_request(
        request: Request, call_next: DispatchCallable
    ) -> Response:
        """Open a profiler for sampled requests and always close it cleanly."""
        if not resolved_config.should_sample():
            setattr(request.state, _STATE_ATTRIBUTE, NullRequestProfiler())
            return await call_next(request)

        profiler = RequestProfiler(tags={"method": request.method})
        setattr(request.state, _STATE_ATTRIBUTE, profiler)

        status = STATUS_OK
        error: str | None = None
        response: Response | None = None
        with bind_profiler(profiler):
            try:
                with profiler.stage(
                    PerformanceStage.RESPONSE, MetricName("dispatch")
                ):
                    response = await call_next(request)
            except asyncio.CancelledError:
                status = STATUS_ERROR
                error = "cancelled"
                raise
            except Exception as exc:
                status = STATUS_ERROR
                error = str(exc)
                raise
            else:
                assert response is not None
                return response
            finally:
                profiler.tags["route"] = _route_template(request)
                if response is not None:
                    profiler.tags["status_code"] = str(response.status_code)
                profile = profiler.complete(status=status, error=error)
                resolved_registry.record_completed_request(profile)


def _route_template(request: Request) -> str:
    """Return the matched route path template, falling back to the raw path.

    Prefers `request.scope["route"].path` (e.g. `/api/query/{id}`) over
    `request.url.path` so requests to the same route with different path
    parameters aggregate under one tag instead of fragmenting per value.
    """
    route = request.scope.get("route")
    path_template = getattr(route, "path", None)
    return path_template or request.url.path


def get_request_profiler(request: Request) -> RequestProfiler | NullRequestProfiler:
    """Retrieve the profiler this middleware attached to `request.state`.

    Returns a `NullRequestProfiler` if the middleware was never installed
    or the request was not sampled, so callers never need an `is None`
    check before using the result.
    """
    profiler: RequestProfiler | NullRequestProfiler | None = getattr(
        request.state, _STATE_ATTRIBUTE, None
    )
    if profiler is None:
        return NullRequestProfiler()
    return profiler
