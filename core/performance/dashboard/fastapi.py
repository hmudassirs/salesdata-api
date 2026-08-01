"""FastAPI wiring for the read-only debug dashboard (Phase 11).

The only FastAPI-dependent module in `dashboard/`, mirroring
`middleware/fastapi.py`'s split: `summary.py` holds the
framework-independent data shaping, this module only wires the routes
onto an `APIRouter` and includes it on the app.

Like the middleware, it knows nothing about auth by default — pass
`dependencies` if the two *data* routes should require it (e.g.
`dependencies=[Depends(require_admin_user)]`), matching `APIRouter`'s
own parameter of that name. Deliberately **not** applied to `{prefix}/`
(the HTML shell page): a browser navigating there has no way to attach
an `x-api-key`/`Authorization` header, so if the shell page required
the same auth as its own data, nobody could ever load it far enough to
see the API-key input box that lets them authenticate its `fetch()`
calls. The shell page itself renders no data — only `{prefix}/performance`
and `{prefix}/request/{{id}}` do, and those stay behind `dependencies`.
See `core.auth.middleware.PUBLIC_PATHS` for the matching exemption at
the auth-middleware level (this router's own dependency isn't the only
wall the shell page has to get past in this app).
"""

from __future__ import annotations

from collections.abc import Sequence

from fastapi import APIRouter, FastAPI, HTTPException
from fastapi.params import Depends
from fastapi.responses import HTMLResponse

from core.performance.dashboard.live import render_dashboard_html
from core.performance.dashboard.summary import (
    DEFAULT_RECENT_REQUESTS_LIMIT,
    build_performance_summary,
)
from core.performance.registry import PerformanceRegistry, get_default_registry


def install_performance_dashboard(
    app: FastAPI,
    registry: PerformanceRegistry | None = None,
    prefix: str = "/debug",
    dependencies: Sequence[Depends] | None = None,
    refresh_interval_seconds: float = 2.0,
) -> None:
    """Register the read-only debug dashboard routes: `{prefix}/`
    (a lightweight, auto-refreshing live HTML view — throughput, latency
    percentiles, pool contention, SQL/trace-stage timing, CPU/memory, and
    recent request traces), `{prefix}/performance` (its JSON data
    source), and `{prefix}/request/{{id}}` (one retained request's full
    trace).

    `registry` defaults to the process-wide default registry — the same
    default `install_performance_middleware` uses — so wiring both with
    no arguments is enough for the dashboard to reflect what the
    middleware recorded.

    `dependencies` gates only `{prefix}/performance` and
    `{prefix}/request/{{id}}` — not `{prefix}/` itself; see the module
    docstring for why.
    """
    resolved_registry = registry or get_default_registry()
    router = APIRouter(prefix=prefix, tags=["performance"])
    data_route_dependencies = list(dependencies or [])

    @router.get("/", response_class=HTMLResponse)
    def get_live_dashboard() -> str:
        """Serve the live, auto-refreshing dashboard page (no auth: static, no data)."""
        return render_dashboard_html(
            prefix=prefix, refresh_interval_seconds=refresh_interval_seconds
        )

    @router.get("/performance", dependencies=data_route_dependencies)
    def get_performance_summary(
        recent_limit: int = DEFAULT_RECENT_REQUESTS_LIMIT,
    ) -> dict[str, object]:
        """Return current counters, gauges, histograms, and recent request history."""
        return build_performance_summary(resolved_registry, recent_limit=recent_limit)

    @router.get("/request/{request_id}", dependencies=data_route_dependencies)
    def get_request_detail(request_id: str) -> dict[str, object]:
        """Return one retained completed request's full trace and metrics."""
        profile = resolved_registry.get_request(request_id)
        if profile is None:
            raise HTTPException(
                status_code=404, detail=f"no retained request with id {request_id!r}"
            )
        return profile.to_dict()

    app.include_router(router)
