"""FastAPI example runner with proper lifecycle management.

Usage:
    python run_api.py
    # Then visit http://localhost:8000/docs for interactive API docs
"""

import asyncio
import concurrent.futures
from contextlib import asynccontextmanager
from pathlib import Path

import uvicorn
from fastapi import FastAPI

from core.app.api.app import create_app
from core.app.lifespan import ApplicationLifespan
from core.db.config import DatabaseConfig, DatabaseSettings
from core.db.init_db import init_database
from core.db.settings import PoolSettings

# Python's asyncio.to_thread() dispatches into the event loop's default
# executor, which — unless explicitly replaced — is a ThreadPoolExecutor
# capped at min(32, os.cpu_count() + 4) workers. That cap is shared
# across EVERY to_thread call in the entire process: every DuckDB query,
# every SQLite service-db call, every observability emit. Under 500
# concurrent requests each making 1-3 to_thread calls, that's up to
# ~1000-1500 jobs competing for ~32 threads — a queue depth that shows
# up directly as multi-second response latency, even though each
# individual operation (confirmed via duckdb_probe.py) only costs tens
# of milliseconds. Replacing the default executor with one sized for
# real expected concurrency removes that artificial ceiling.
#
# 300 is a starting point, not a law of physics — tune based on actual
# expected peak concurrency and available memory (each thread reserves
# stack space; thousands of workers is a different, worse problem).
_IO_EXECUTOR = concurrent.futures.ThreadPoolExecutor(
    max_workers=300, thread_name_prefix="io-worker"
)


async def main():
    """Run FastAPI development server with proper lifecycle management."""
    asyncio.get_running_loop().set_default_executor(_IO_EXECUTOR)

    project_root = Path(__file__).resolve().parent
    data_dir = project_root / "data"
    data_dir.mkdir(parents=True, exist_ok=True)

    # Initialize database with api_keys table
    db_path = str(data_dir / "mydatabase.duckdb")
    # init_database(db_path)
    # print(f"✓ Database initialized: {db_path}")

    # Path for the auxiliary SQLite service database (api keys, users,
    # logging, tracing, caching, audit). Connecting, creating tables,
    # seeding the admin user, and running startup cleanup all happen
    # inside ApplicationLifespan's ServiceDatabaseStep — nothing to do
    # here except hand over the path.
    service_db_path = str(data_dir / "service.db")

    pool_config = PoolSettings(min_size=2, max_size=10, timeout=30)
    db_settings = DatabaseSettings(pool=pool_config)
    db_config = DatabaseConfig.from_duckdb(db_path, settings=db_settings)

    # Create lifespan manager (async mode for pooled database access)
    lifespan_mgr = ApplicationLifespan(
        db_config, service_db_path=service_db_path, mode="async"
    )

    # This runner is async-only: every route in core/app/api/routes.py is
    # `async def` and calls db_session.get_async_session() / check_async(),
    # not the sync equivalents. ApplicationLifespan.mode defaults to
    # "sync" (it's shared infra also used by non-async callers), so a
    # future edit here that drops the explicit mode="async" kwarg above
    # would silently put this server back in sync mode — and the DB
    # pool mismatch wouldn't surface until the first request that
    # touches the database. Fail here, at startup, instead of there.
    assert lifespan_mgr.mode == "async", (
        "run_api.py's routes require ApplicationLifespan(mode='async'); "
        f"got mode={lifespan_mgr.mode!r}"
    )

    # Define FastAPI lifespan context manager
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        """FastAPI lifespan: startup and shutdown."""
        # Startup phase
        await lifespan_mgr.startup_async()

        # Get db_session after startup and attach to app.state
        db_session = lifespan_mgr.get_db_session()
        app.state.db_session = db_session

        # Service manager was built by ApplicationLifespan's
        # ServiceDatabaseStep during startup_async() above — reuse it
        # rather than constructing a second connection. Note
        # ServiceDatabaseStep.startup_async() still runs the sqlite3
        # (blocking) setup via asyncio.to_thread internally, so this is
        # safe to call from an async context.
        app.state.service_manager = lifespan_mgr.get_service_manager()
        app.state.container = lifespan_mgr.get_container()

        if db_session and db_session._async_pool:
            created = await db_session.warmup_async()
            print(f"✓ Pool warmed up: {created} connections pre-created")
            print(f"📊 Pool metrics: {db_session._async_pool.metrics()}")

        print("\n" + "=" * 60)
        print("🚀 PrepareData API Server")
        print("=" * 60)
        print("📍 Starting on http://localhost:8000")
        print("📚 API Docs: http://localhost:8000/docs")
        print("🔍 ReDoc: http://localhost:8000/redoc")
        print("🔑 API Keys: http://localhost:8000/docs#/authentication")
        print("=" * 60 + "\n")

        yield

        # Shutdown phase
        await lifespan_mgr.shutdown_async()

    # Create FastAPI app with lifespan context manager and observability middleware
    app = create_app(
        title="PrepareData API",
        version="1.0.0",
        description="Database preparation and management API",
    )
    app.router.lifespan_context = lifespan

    # Run server
    config = uvicorn.Config(
        app,
        host="127.0.0.1",
        port=8000,
        log_level="info",
        reload=False,
    )
    server = uvicorn.Server(config)
    await server.serve()


if __name__ == "__main__":
    asyncio.run(main())
