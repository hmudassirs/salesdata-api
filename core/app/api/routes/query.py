"""Data-query routes: health, /api/query, table introspection."""

import asyncio

from fastapi import APIRouter, HTTPException, Request

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
from core.observability.context import build_request_context
from core.db.session import DatabaseSession

router = APIRouter(prefix="/api", tags=["database"])

# asyncio.create_task()'s result must be kept referenced somewhere, or
# the task can be garbage-collected before it finishes running — a
# well-known asyncio footgun. This set exists purely to hold those
# references until each task completes, then discards itself.
_background_tasks: set = set()


def _fire_and_forget(coro) -> None:
    """Schedule `coro` to run in the background without the caller
    waiting for it. Any exception is logged, not raised — by
    definition, nothing is watching this task's result."""
    import logging

    task = asyncio.ensure_future(coro)
    _background_tasks.add(task)

    def _on_done(t: asyncio.Task) -> None:
        _background_tasks.discard(t)
        exc = t.exception() if not t.cancelled() else None
        if exc:
            logging.getLogger(__name__).warning("Background task failed", exc_info=exc)

    task.add_done_callback(_on_done)


@router.get("/health", response_model=HealthResponse)
async def health_check(db_session: DatabaseSession = GetDB) -> HealthResponse:
    """Check API and database health status.

    Uses `check_async()`, not `check_sync()`: the app's lifespan now runs
    in async mode and initializes an async connection pool
    (`DataWarehouseStep.startup_async()` -> `db_session.initialize()`),
    so `check_sync()` would reach for a sync pool that was never created.
    `check_async()` is a real coroutine, so it must be awaited.

    Returns:
        HealthResponse with status and pool metrics
    """
    try:
        health = HealthCheck(db_session)
        status_dict = await health.check_async()

        pool_metrics = None
        if getattr(db_session, "_async_pool", None):
            pool_metrics = db_session._async_pool.metrics()

        return HealthResponse(
            status=status_dict.get("status", "unhealthy"),
            db_connected=bool(status_dict.get("database", False)),
            pool_metrics=pool_metrics,
        )
    except Exception:
        return HealthResponse(
            status="unhealthy",
            db_connected=False,
            pool_metrics=None,
        )


@router.post("/query", response_model=QueryResponse)
async def execute_query(
    request: Request,
    query: QueryRequest,
    db_session: DatabaseSession = GetDB,
    service_manager=GetServiceManager,
    current_user: CurrentUser = GetCurrentUser,
) -> QueryResponse:
    """Execute a SQL query with caching.

    WARNING: This executes arbitrary caller-supplied SQL with no
    statement-type restriction — any authenticated caller can run
    INSERT/UPDATE/DELETE/DDL, not just SELECT. If this is meant to be a
    read-only query console, add an explicit check that
    `query.sql.strip().upper().startswith("SELECT")` (or a proper SQL
    parser check) before execution, and consider gating write-capable
    access behind an admin/elevated-scope check. Left as-is here since
    unrestricted execution may be an intentional "DB console" feature —
    confirm this is the intended threat model before shipping.

    Args:
        query: QueryRequest with SQL and optional parameters

    Returns:
        QueryResponse with results or error
    """
    try:
        build_request_context(request)

        # Generate cache key
        params = tuple(query.params or [])
        cache_key = service_manager.caching.generate_cache_key(query.sql, params)

        # Check cache first (only for SELECT queries)
        if query.sql.strip().upper().startswith("SELECT"):
            cached_result = await asyncio.to_thread(
                service_manager.caching.get_cached_result, cache_key
            )
            if cached_result:
                import json

                result_data = json.loads(cached_result["result_data"])
                return QueryResponse(
                    success=True,
                    data=result_data,
                    row_count=cached_result["result_count"],
                    error=None,
                    cached=True,
                )

        # Execute query
        async with db_session.get_async_session() as db:
            results = await db.fetch_all(query.sql, params)

        # Cache SELECT results — fire-and-forget. The client already has
        # their results; making them wait for a write that only helps
        # the *next* request to run this query is pure added latency
        # with no benefit to this response. Also matters under a cache
        # "stampede": if many concurrent requests run the same
        # not-yet-cached query at once, they'd otherwise all block on
        # the same synchronous cache write serializing behind SQLite's
        # single-writer lock — now they just each schedule a background
        # write and return immediately.
        if query.sql.strip().upper().startswith("SELECT") and results:
            _fire_and_forget(
                asyncio.to_thread(
                    service_manager.caching.cache_result,
                    query_sql=query.sql,
                    result_data=results,
                    params=params,
                    execution_time_ms=0,  # Could be measured if needed
                    user_id=current_user.user_id or None,
                )
            )

        return QueryResponse(
            success=True,
            data=results,
            row_count=len(results),
            error=None,
            cached=False,
        )
    except Exception as e:
        return QueryResponse(
            success=False,
            data=None,
            error=str(e),
            row_count=0,
            cached=False,
        )


@router.get("/tables", response_model=TablesResponse)
async def list_tables(db_session: DatabaseSession = GetDB) -> TablesResponse:
    """Get list of all tables in the database.

    Returns:
        TablesResponse with table names and count
    """
    try:
        tables = db_session._adapter.get_tables()

        return TablesResponse(
            tables=tables,
            count=len(tables),
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to list tables: {str(e)}")


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
        known_tables = set(db_session._adapter.get_tables())
        if table_name not in known_tables:
            raise HTTPException(status_code=404, detail=f"Unknown table: {table_name}")

        schema = db_session._adapter.get_schema(table_name)

        return {
            "table": table_name,
            "columns": schema,
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(
            status_code=404,
            detail=f"Table or schema not found: {str(e)}",
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
        known_tables = set(db_session._adapter.get_tables())
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
    except Exception as e:
        raise HTTPException(
            status_code=404,
            detail=f"Failed to count table: {str(e)}",
        )


# ============================================================================
# API KEY MANAGEMENT ROUTES
# ============================================================================
