"""FastAPI example runner with proper lifecycle management.

Usage:
    python run_api.py
    # Then visit http://localhost:8000/docs for interactive API docs
"""

import asyncio
import concurrent.futures
import os
from contextlib import asynccontextmanager
from pathlib import Path

import uvicorn
from fastapi import FastAPI

from core.app.api.app import create_app
from core.app.lifespan import ApplicationLifespan
from core.concurrency.cpu import recommended_sizing
from core.concurrency.executors import configure_executors
from core.db.config import DatabaseConfig, DatabaseSettings
from core.db.init_db import init_database
from core.db.settings import PoolSettings

# Historical note: this used to set ONE 300-worker default executor via
# `asyncio.get_running_loop().set_default_executor(...)` and route every
# blocking call in the process through it via `asyncio.to_thread`. That
# fixed the immediate "only 32 threads for everything" ceiling, but still
# left DuckDB warehouse queries, SQLite service-db calls (auth, query
# cache), and fire-and-forget background writes all competing for the
# same pool of threads -- a burst of slow warehouse queries could still
# starve API-key validation, purely because they share a thread pool
# that has nothing to do with either workload (roadmap rule #5: no one
# oversized global thread pool for unrelated workloads).
#
# `configure_executors()` (core/concurrency/executors.py) replaces this
# with three separate bounded pools -- db / service / background -- each
# sized to the connection pool it actually fronts, so one workload's
# load can't starve the others' threads. A small default executor is
# still set for the handful of one-off startup/shutdown calls
# (`core/app/lifespan.py`) and observability's synchronous fallback path
# that don't go through any of the three dedicated executors.
_DEFAULT_EXECUTOR = concurrent.futures.ThreadPoolExecutor(
    max_workers=max(4, recommended_sizing().cpu_count * 2),
    thread_name_prefix="default-io-worker",
)


async def main():
    """Run FastAPI development server with proper lifecycle management."""
    asyncio.get_running_loop().set_default_executor(_DEFAULT_EXECUTOR)

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

    # Pool/executor sizes are derived from the actual CPU count available
    # to this process (core/concurrency/cpu.py) rather than hardcoded --
    # this used to be a flat `PoolSettings(min_size=2, max_size=10)`
    # picked for whatever machine last ran the load test, which either
    # starves a bigger box or oversubscribes a smaller one. Each
    # individual number can still be overridden with an env var (e.g. for
    # a production deployment that's benchmarked its own optimal size);
    # what's dynamic is the *default* when nothing more specific is set.
    sizing = recommended_sizing()
    print(
        f"🧮 Detected {sizing.cpu_count} usable CPU(s) -- deriving pool/executor "
        f"sizes from that (override with env vars to pin explicit values)"
    )

    pool_min = int(os.getenv("DB_POOL_MIN_SIZE", str(sizing.db_pool_min)))
    pool_max = int(os.getenv("DB_POOL_MAX_SIZE", str(sizing.db_pool_max)))
    pool_config = PoolSettings(min_size=pool_min, max_size=pool_max, timeout=30)
    db_settings = DatabaseSettings(pool=pool_config)
    db_config = DatabaseConfig.from_duckdb(db_path, settings=db_settings)

    service_pool_min = int(
        os.getenv("SERVICE_DB_POOL_MIN_SIZE", str(sizing.service_pool_min))
    )
    service_pool_max = int(
        os.getenv("SERVICE_DB_POOL_MAX_SIZE", str(sizing.service_pool_max))
    )

    # Size the DB/service executors off the *actual* pool configuration
    # above, plus a little headroom for in-flight reservation
    # bookkeeping -- there's no benefit to more worker threads than
    # there are connections for them to use. `service_workers` covers
    # ServiceDatabase's own pool (see core/storage/service_db.py).
    configure_executors(
        db_workers=int(
            os.getenv("DB_EXECUTOR_WORKERS", str(pool_max + 2))
        ),
        service_workers=int(
            os.getenv("SERVICE_EXECUTOR_WORKERS", str(service_pool_max + 2))
        ),
        background_workers=int(
            os.getenv(
                "BACKGROUND_EXECUTOR_WORKERS",
                str(sizing.background_executor_workers),
            )
        ),
    )

    # Create lifespan manager (async mode for pooled database access)
    lifespan_mgr = ApplicationLifespan(
        db_config,
        service_db_path=service_db_path,
        mode="async",
        service_pool_min_size=service_pool_min,
        service_pool_max_size=service_pool_max,
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
