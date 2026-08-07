"""Data-query routes: health, /api/query, table introspection."""

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse

from core.app.api.dependencies import (
    CurrentUser,
    GetCurrentUser,
    GetDB,
    GetServiceManager,
)
from core.app.api.schemas import (
    HealthResponse,
    QueryRequest,
    QueryResponse,
    TablesResponse,
)
from core.app.health import HealthCheck
from core.app.settings import AppSettings
from core.observability.context import build_request_context
from core.concurrency.executors import all_executor_metrics
from core.caching.persistence_queue import persistence_metrics
from core.db.logger import get_logger
from core.observability.alerts import get_alert_evaluator
from core.db.session import DatabaseSession
from core.services.query_limits import query_concurrency_metrics
from core.services.query_service import QueryAuthorizationError, QueryService

logger = get_logger(__name__)

router = APIRouter(prefix="/api", tags=["database"])


@router.get("/health", response_model=HealthResponse)
async def health_check(
    request: Request, db_session: DatabaseSession = GetDB
) -> HealthResponse:
    """Check API and database health status.

    Uses `check_async()`, not `check_sync()`: the app's lifespan now runs
    in async mode and initializes an async connection pool
    (`DataWarehouseStep.startup_async()` -> `db_session.initialize()`),
    so `check_sync()` would reach for a sync pool that was never created.
    `check_async()` is a real coroutine, so it must be awaited.

    Also surfaces executor and query-concurrency-semaphore metrics
    (roadmap P0-1: "instrument pool wait and executor wait") alongside
    the connection pool's own metrics, adaptive-sampler state (P1-4),
    and a list of currently-firing operational alerts evaluated against
    all of the above (P1-5), so pool/executor/concurrency/sampling
    pressure -- and whether any of it currently warrants attention --
    can all be read from one place.

    Returns:
        HealthResponse with status, pool/executor/concurrency/
        persistence/sampler metrics, and active alerts
    """
    try:
        health = HealthCheck(db_session)
        status_dict = await health.check_async()

        pool_metrics = None
        if getattr(db_session, "_async_pool", None):
            pool_metrics = db_session._async_pool.metrics()

        executor_metrics = all_executor_metrics()
        concurrency_metrics = query_concurrency_metrics()
        cache_metrics = persistence_metrics()
        sampler = getattr(request.app.state, "adaptive_sampler", None)
        sampler_metrics = sampler.metrics() if sampler is not None else None

        alerts = get_alert_evaluator().evaluate(
            pool_metrics=pool_metrics,
            executor_metrics=executor_metrics,
            query_concurrency_metrics=concurrency_metrics,
            cache_persistence_metrics=cache_metrics,
            adaptive_sampler_metrics=sampler_metrics,
        )

        return HealthResponse(
            status=status_dict.get("status", "unhealthy"),
            db_connected=bool(status_dict.get("database", False)),
            pool_metrics=pool_metrics,
            executor_metrics=executor_metrics,
            query_concurrency_metrics=concurrency_metrics,
            cache_persistence_metrics=cache_metrics,
            adaptive_sampler_metrics=sampler_metrics,
            alerts=alerts,
        )
    except Exception:
        return HealthResponse(
            status="unhealthy",
            db_connected=False,
            pool_metrics=None,
            executor_metrics=None,
            query_concurrency_metrics=None,
            cache_persistence_metrics=None,
            adaptive_sampler_metrics=None,
            alerts=[],
        )


@router.get("/dashboard", response_class=HTMLResponse)
async def operational_dashboard() -> HTMLResponse:
    """Minimal operational dashboard (roadmap P1-5).

    Server-rendered static shell that polls `GET /api/health` (this
    same auth context -- the browser tab making the request must carry
    a valid session/API key, same as any other endpoint) every few
    seconds and renders the pool/executor/query-concurrency/cache-
    persistence/sampler metrics plus any currently-firing alerts. No
    build step, no separate frontend app, no external JS dependency --
    deliberately small enough to read top to bottom.
    """
    return HTMLResponse(content=_DASHBOARD_HTML)


_DASHBOARD_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>PrepareData API -- Operational Dashboard</title>
<style>
  body { font-family: system-ui, sans-serif; background: #0b0e14; color: #d7dde5; margin: 0; padding: 24px; }
  h1 { font-size: 1.1rem; color: #8fb4ff; margin-bottom: 4px; }
  .subtitle { color: #6b7684; font-size: 0.8rem; margin-bottom: 20px; }
  .grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(260px, 1fr)); gap: 14px; }
  .card { background: #131722; border: 1px solid #232838; border-radius: 8px; padding: 14px 16px; }
  .card h2 { font-size: 0.85rem; color: #9aa4b2; margin: 0 0 10px 0; text-transform: uppercase; letter-spacing: 0.04em; }
  .row { display: flex; justify-content: space-between; padding: 2px 0; font-size: 0.85rem; }
  .row .k { color: #8b93a1; }
  .row .v { color: #e6ebf1; font-variant-numeric: tabular-nums; }
  .alerts { margin-bottom: 16px; }
  .alert { border-radius: 6px; padding: 8px 12px; margin-bottom: 6px; font-size: 0.85rem; }
  .alert.warning { background: #3a2f10; border: 1px solid #8a6d1a; color: #f0cf6b; }
  .alert.critical { background: #3a1414; border: 1px solid #8a2a2a; color: #f28b8b; }
  .ok { color: #6bdc8f; font-size: 0.85rem; }
  #updated { color: #555f6e; font-size: 0.75rem; }
</style>
</head>
<body>
<h1>PrepareData API &mdash; Operational Dashboard</h1>
<div class="subtitle">Auto-refreshes every 3s from <code>GET /api/health</code> &middot; <span id="updated">not yet loaded</span></div>
<div id="alerts" class="alerts"></div>
<div id="grid" class="grid"></div>

<script>
function row(k, v) {
  return `<div class="row"><span class="k">${k}</span><span class="v">${v}</span></div>`;
}
function card(title, rows) {
  return `<div class="card"><h2>${title}</h2>${rows}</div>`;
}
async function refresh() {
  try {
    const res = await fetch('/api/health');
    const h = await res.json();

    const alertsEl = document.getElementById('alerts');
    if (h.alerts && h.alerts.length) {
      alertsEl.innerHTML = h.alerts.map(a =>
        `<div class="alert ${a.severity}"><strong>${a.code}</strong> -- ${a.message}</div>`
      ).join('');
    } else {
      alertsEl.innerHTML = '<div class="ok">No active alerts</div>';
    }

    const cards = [];
    if (h.pool_metrics) {
      const p = h.pool_metrics;
      cards.push(card('DB Connection Pool', [
        row('size (min/max)', `${p.min_connections} / ${p.max_connections}`),
        row('active / idle', `${p.active_connections} / ${p.idle_connections}`),
        row('queue depth', p.queue_depth),
        row('utilization', (p.utilization*100).toFixed(0)+'%'),
        row('saturation', (p.saturation*100).toFixed(0)+'%'),
        row('p50 / p95 / p99 wait (ms)', `${p.p50_wait_time_ms ?? '-'} / ${p.p95_wait_time_ms ?? '-'} / ${p.p99_wait_time_ms ?? '-'}`),
        row('timeouts', p.timed_out_acquires),
        row('broken evicted', p.broken_connections_evicted ?? 0),
      ].join('')));
    }
    if (h.executor_metrics) {
      for (const [name, e] of Object.entries(h.executor_metrics)) {
        cards.push(card(`Executor: ${name}`, [
          row('workers', e.max_workers),
          row('active', e.active),
          row('queued', e.approx_queue_depth),
        ].join('')));
      }
    }
    if (h.query_concurrency_metrics) {
      const rows = Object.entries(h.query_concurrency_metrics).map(([cost, m]) =>
        row(cost, `${m.in_use} / ${m.limit}`)
      ).join('');
      cards.push(card('Query Concurrency', rows));
    }
    if (h.cache_persistence_metrics) {
      const c = h.cache_persistence_metrics;
      cards.push(card('Cache Persistence Queue', [
        row('queue depth', c.queue_depth),
        row('enqueued', c.enqueued),
        row('completed', c.completed),
        row('failed', c.failed),
        row('dropped', c.dropped),
        row('avg latency (ms)', c.avg_latency_ms),
      ].join('')));
    }
    if (h.adaptive_sampler_metrics) {
      const s = h.adaptive_sampler_metrics;
      cards.push(card('Adaptive Sampler', [
        row('current rate', s.current_rate_percent + '%'),
        row('target samples/sec', s.target_samples_per_second),
        row('escalated', s.escalated),
        row('total sampled / requests', `${s.total_sampled} / ${s.total_requests}`),
      ].join('')));
    }
    cards.push(card('Status', [
      row('status', h.status),
      row('db connected', h.db_connected),
    ].join('')));

    document.getElementById('grid').innerHTML = cards.join('');
    document.getElementById('updated').textContent = 'last updated ' + new Date().toLocaleTimeString();
  } catch (e) {
    document.getElementById('updated').textContent = 'failed to load: ' + e;
  }
}
refresh();
setInterval(refresh, 3000);
</script>
</body>
</html>
"""


@router.post("/query", response_model=QueryResponse)
async def execute_query(
    request: Request,
    query: QueryRequest,
    db_session: DatabaseSession = GetDB,
    service_manager=GetServiceManager,
    current_user: CurrentUser = GetCurrentUser,
) -> QueryResponse:
    """Execute a SQL query with caching.

    Execution policy (roadmap Phase 13.1): this is an authenticated "DB
    console", not a read-only reporting API -- SELECT/WITH is always
    allowed; INSERT/UPDATE/DELETE/DDL/etc. additionally requires the
    caller's "write" scope (see core.db.sql_policy, core.app.settings).
    A write attempt without that scope raises QueryAuthorizationError,
    surfaced here as 403.

    All caching, single-flight coalescing, cost-class concurrency
    gating, result-size limits, and cache invalidation-on-write live in
    `QueryService` -- this handler is just request/response translation
    plus the authorization-error -> HTTP-status mapping (roadmap P0-9).

    Args:
        query: QueryRequest with SQL and optional parameters

    Returns:
        QueryResponse with results or error
    """
    build_request_context(request)

    params = tuple(query.params or [])
    settings: AppSettings = request.app.state.settings
    service = QueryService(
        db_session,
        service_manager.query_cache,
        require_write_scope=settings.require_write_scope_for_mutations,
        precise_cache_invalidation=settings.cache_invalidation_precise,
    )

    try:
        outcome = await service.run(
            query.sql,
            params,
            user_id=current_user.user_id,
            scopes=current_user.scopes,
        )
    except QueryAuthorizationError as e:
        raise HTTPException(status_code=403, detail=str(e))
    except Exception as e:
        return QueryResponse(
            success=False,
            data=None,
            error=str(e),
            row_count=0,
            cached=False,
            truncated=False,
        )

    return QueryResponse(
        success=True,
        data=outcome.data,
        row_count=len(outcome.data),
        error=None,
        cached=outcome.cached,
        truncated=outcome.truncated,
    )


@router.get("/tables", response_model=TablesResponse)
async def list_tables(db_session: DatabaseSession = GetDB) -> TablesResponse:
    """Get list of all tables in the database.

    Returns:
        TablesResponse with table names and count
    """
    try:
        # `DatabaseSession.get_tables()` offloads the blocking adapter
        # call to the service executor -- calling `db_session._adapter
        # .get_tables()` directly here used to run it synchronously on
        # the event loop, blocking every other in-flight request for
        # the duration of the call.
        tables = await db_session.get_tables()

        return TablesResponse(
            tables=tables,
            count=len(tables),
        )
    except Exception:
        logger.exception("Failed to list tables")
        raise HTTPException(status_code=500, detail="Failed to list tables")


@router.get("/tables/{table_name}/schema")
async def get_table_schema(
    table_name: str,
    db_session: DatabaseSession = GetDB,
) -> dict:
    """Get schema information for a table.

    Args:
        table_name: Name of the table (e.g., 'users', 'orders')

    Returns:
        Table schema information

    Example:
        GET /api/tables/users/schema
    """
    try:
        known_tables = set(await db_session.get_tables())
        if table_name not in known_tables:
            raise HTTPException(status_code=404, detail=f"Unknown table: {table_name}")

        schema = await db_session.get_schema(table_name)

        return {
            "table": table_name,
            "columns": schema,
        }
    except HTTPException:
        raise
    except Exception:
        logger.exception("Failed to fetch schema for table %s", table_name)
        raise HTTPException(
            status_code=404,
            detail=f"Table or schema not found: {table_name}",
        )


@router.get("/tables/{table_name}/count")
async def get_table_count(
    table_name: str,
    db_session: DatabaseSession = GetDB,
) -> dict:
    """Get row count for a table.

    Args:
        table_name: Name of the table (e.g., 'users', 'orders')

    Returns:
        Row count

    Example:
        GET /api/tables/users/count
    """
    try:
        # table_name cannot be parameterized as a bind variable (it's an
        # identifier, not a value), so it must instead be validated
        # against the real set of tables before being interpolated.
        # Previously this went straight into an f-string with no check
        # at all — a direct SQL-injection path via the URL path segment.
        known_tables = set(await db_session.get_tables())
        if table_name not in known_tables:
            raise HTTPException(status_code=404, detail=f"Unknown table: {table_name}")

        async with db_session.get_async_session() as db:
            results = await db.fetch_all(
                f"SELECT COUNT(*) as count FROM {table_name}", ()
            )

        row_count = results[0]["count"] if results else 0

        return {
            "table": table_name,
            "row_count": row_count,
        }
    except HTTPException:
        raise
    except Exception:
        logger.exception("Failed to count rows for table %s", table_name)
        raise HTTPException(
            status_code=404,
            detail=f"Failed to count table: {table_name}",
        )


# ============================================================================
# API KEY MANAGEMENT ROUTES
# ============================================================================
