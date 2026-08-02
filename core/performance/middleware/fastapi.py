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
import time
from collections.abc import Awaitable, Callable

from fastapi import FastAPI, Request, Response

from core.performance.adaptive_sampler import AdaptiveSampler, AdaptiveSamplerConfig
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
_SAMPLER_STATE_ATTRIBUTE = "performance_adaptive_sampler"

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

    When `config.adaptive_sampling` is set, the sampling decision comes
    from an `AdaptiveSampler` (core/performance/adaptive_sampler.py)
    instead of `config`'s fixed `sample_rate_percent` -- see that
    module's docstring for what "adaptive" means and its honest
    limitations around retroactively capturing unsampled requests.
    """
    resolved_config = config or PerformanceConfig.from_env()
    resolved_registry = registry or get_default_registry()

    sampler: AdaptiveSampler | None = None
    if resolved_config.adaptive_sampling:
        sampler = AdaptiveSampler(
            AdaptiveSamplerConfig(
                target_samples_per_second=resolved_config.target_samples_per_second,
                min_rate_percent=resolved_config.min_sample_rate_percent,
                max_rate_percent=resolved_config.max_sample_rate_percent,
                slow_request_threshold_seconds=resolved_config.slow_request_threshold_seconds,
                escalation_seconds=resolved_config.escalation_seconds,
            )
        )
        app.state.adaptive_sampler = sampler

    @app.middleware("http")
    async def profile_request(
        request: Request, call_next: DispatchCallable
    ) -> Response:
        """Open a profiler for sampled requests and always close it
        cleanly. Unconditionally times every request (sampled or not)
        so `sampler.record_outcome()` can still see slow/error outcomes
        for requests that weren't sampled -- see AdaptiveSampler's
        docstring for why that's the mechanism, not a full retroactive
        trace."""
        was_sampled = (
            sampler.should_sample()
            if sampler is not None
            else resolved_config.should_sample()
        )

        request_start = time.perf_counter()

        if not was_sampled:
            setattr(request.state, _STATE_ATTRIBUTE, NullRequestProfiler())
            status_code = 500
            try:
                response = await call_next(request)
                status_code = response.status_code
                return response
            finally:
                if sampler is not None:
                    sampler.record_outcome(
                        status_code=status_code,
                        duration_seconds=time.perf_counter() - request_start,
                        was_sampled=False,
                    )

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
                if sampler is not None:
                    sampler.record_outcome(
                        status_code=(
                            response.status_code if response is not None else 500
                        ),
                        duration_seconds=time.perf_counter() - request_start,
                        was_sampled=True,
                    )


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
