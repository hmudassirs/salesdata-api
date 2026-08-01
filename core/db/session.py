from __future__ import annotations

import asyncio
import time
from contextlib import asynccontextmanager, contextmanager
from typing import Any, Optional, Protocol, Sequence

from core.db.logger import get_logger

# Optional observability integrations
try:
    from core.observability import (
        APP_LATENCY,
        POOL_ACTIVE,
        POOL_REQUESTS,
        POOL_TIMEOUTS,
        POOL_WAIT,
    )
except Exception:
    POOL_REQUESTS = POOL_WAIT = POOL_ACTIVE = POOL_TIMEOUTS = APP_LATENCY = None

# Optional OpenTelemetry tracer. Routed through core.observability.otel's
# OpenTelemetryManager rather than calling `opentelemetry.trace.get_tracer()`
# directly — the direct call was the bug: it produces a real Tracer object,
# but without something calling `trace.set_tracer_provider(...)` first
# (which only OpenTelemetryManager.initialize() ever does), that tracer has
# no exporter wired up and every span it creates goes nowhere. Getting the
# tracer from the manager guarantees .initialize() has run first.
try:
    from core.observability.otel import get_otel_manager

    _TRACER = get_otel_manager().tracer
except Exception:
    _TRACER = None

import core.db.adapters  # noqa: F401 — side effect: each adapter module

# self-registers with the AdapterRegistry on import (see
# core/db/adapters/__init__.py and each adapter's bottom-of-file
# `get_adapter_registry().register(...)` call). session.py deliberately
# never imports DuckDBAdapter/SQLiteAdapter by name — see _create_adapter().
from core.db.config import DatabaseConfig, DatabaseType
from core.db.pool import AsyncConnectionPool, SyncConnectionPool
from core.db.pool.base import MaxConnectionsExceeded
from core.db.registry import get_adapter_registry

# Optional core.performance integration: pool acquire/release timing +
# pool contention gauges (adapters.pool), per-connection SQL execute/
# fetch timing (adapters.sql_adapter), and an opt-in instrumented
# transaction helper (adapters.transactions) — see
# docs/performance/adapters.md. Guarded the same way the
# core.observability integration above is: core.db must keep working
# with core.performance absent.
try:
    from core.performance.adapters.pool import (
        AsyncPoolTimingAdapter,
        SyncPoolTimingAdapter,
    )
    from core.performance.adapters.sql_adapter import InstrumentedSQLAdapter
    from core.performance.adapters.transactions import (
        instrumented_async_transaction,
        instrumented_transaction,
    )
except Exception:
    AsyncPoolTimingAdapter = SyncPoolTimingAdapter = None  # type: ignore[assignment,misc]
    InstrumentedSQLAdapter = None  # type: ignore[assignment,misc]
    instrumented_async_transaction = instrumented_transaction = None  # type: ignore[assignment]

logger = get_logger(__name__)


from core.db.protocols import DatabaseAdapter as DBAdapter

# ---------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------


async def _run_in_thread(fn, *args):
    """Centralized async → thread execution with optional metrics and tracing.

    Measures elapsed time and records it to `APP_LATENCY` when available.
    If OpenTelemetry is configured, wraps the call in a span named
    `db.{fn.__name__}` (e.g. `db.execute`, `db.fetch_all`) — previously
    every call through here used the single generic name `db.sync_call`,
    so a trace couldn't tell an execute() from a fetch_all() from a
    disconnect(). The first positional arg is attached as `db.statement`
    when it looks like SQL, truncated to keep spans small.
    """
    start = time.perf_counter()

    if _TRACER:
        span_name = f"db.{getattr(fn, '__name__', 'call')}"
        with _TRACER.start_as_current_span(span_name) as span:
            if args and isinstance(args[0], str):
                span.set_attribute("db.statement", args[0][:200])
            result = await asyncio.to_thread(fn, *args)
    else:
        result = await asyncio.to_thread(fn, *args)

    elapsed = time.perf_counter() - start
    if APP_LATENCY is not None:
        try:
            APP_LATENCY.observe(elapsed)
        except Exception:
            logger.debug("Failed to record APP_LATENCY", exc_info=True)

    return result


def _safe_rollback(adapter: DBAdapter) -> None:
    try:
        if getattr(adapter, "connection", None):
            adapter.connection.rollback()
    except Exception:
        logger.debug("Rollback failed", exc_info=True)


# ---------------------------------------------------------------------
# Session wrappers
# ---------------------------------------------------------------------


class AsyncSessionWrapper:
    """Async-friendly wrapper over a sync adapter."""

    __slots__ = ("_adapter",)

    def __init__(self, adapter: DBAdapter):
        self._adapter = adapter

    async def execute(self, sql: str, params=None):
        return await _run_in_thread(self._adapter.execute, sql, params or ())

    async def fetch_all(self, sql: str, params=None):
        return await _run_in_thread(self._adapter.fetch_all, sql, params or ())

    async def fetch_one(self, sql: str, params=None):
        return await _run_in_thread(self._adapter.fetch_one, sql, params or ())

    async def rollback(self):
        await _run_in_thread(_safe_rollback, self._adapter)

    async def close(self):
        await _run_in_thread(self._adapter.disconnect)


class SyncSessionWrapper:
    """Sync wrapper over adapter."""

    __slots__ = ("_adapter",)

    def __init__(self, adapter: DBAdapter):
        self._adapter = adapter

    def execute(self, sql: str, params=None):
        return self._adapter.execute(sql, params or ())

    def fetch_all(self, sql: str, params=None):
        return self._adapter.fetch_all(sql, params or ())

    def fetch_one(self, sql: str, params=None):
        return self._adapter.fetch_one(sql, params or ())

    def rollback(self):
        _safe_rollback(self._adapter)

    def close(self):
        self._adapter.disconnect()


# ---------------------------------------------------------------------
# DatabaseSession
# ---------------------------------------------------------------------


class DatabaseSession:
    """Manages pooled sync & async database sessions."""

    def __init__(self, config: DatabaseConfig):
        self.config = config
        self._adapter: Optional[DBAdapter] = None
        self._async_pool: Optional[AsyncConnectionPool] = None
        self._sync_pool: Optional[SyncConnectionPool] = None
        # core.performance-wrapped pools, used by get_async_session()/
        # get_sync_session() and get_async_transaction()/
        # get_sync_transaction(). Falls back to the raw pool itself when
        # core.performance isn't installed (see the try/except import
        # above), so callers can always go through `self._async_pool_adapter`
        # / `self._sync_pool_adapter` uniformly.
        self._async_pool_adapter: Optional[Any] = None
        self._sync_pool_adapter: Optional[Any] = None

    # ------------------ adapter factory ------------------

    def _create_adapter(self) -> DBAdapter:
        """Look up the adapter class for this config's db_type via the
        registry instead of a hardcoded if/else — adding a new backend
        means registering it in its own adapters/*.py file (see
        core/db/adapters/__init__.py), not editing this method.

        Note this solves "which class", not "which constructor
        arguments": DuckDB and SQLite both happen to take
        (path, echo=...), so a uniform call works today. A backend with
        a genuinely different constructor (e.g. Postgres's host/port/
        user/password) would need either its own branch here or a
        per-adapter `from_config()` classmethod — the registry doesn't
        make that problem disappear, it just means today's two backends
        don't need session.py touched to add a third that fits the same
        shape.
        """
        db_type = self.config.db_type.value  # "duckdb" / "sqlite"
        try:
            adapter_class = get_adapter_registry().get_adapter_class(db_type)
        except ValueError as e:
            raise ValueError(
                f"Unsupported database type configured: {db_type!r}"
            ) from e

        prefix = f"{db_type}:///"
        path = self.config.connection_string
        if path.startswith(prefix):
            path = path[len(prefix) :]

        return adapter_class(path, echo=getattr(self.config, "echo", False))

    # ------------------ initialization ------------------

    async def initialize(self) -> None:
        # A dedicated, unpooled adapter for introspection-only calls
        # (get_tables()) that don't go through get_async_session().
        self._adapter = self._create_adapter()
        await _run_in_thread(self._adapter.connect)

        async def create_session():
            # IMPORTANT: build a fresh adapter (and open its own connection)
            # per call, not a closure over one shared adapter. Every
            # connection this pool hands out must be physically
            # independent — sharing one connection across concurrently
            # -used pooled "slots" corrupts it under concurrent access
            # (this is the same class of bug found and fixed in
            # ServiceDatabase's SQLite layer).
            pooled_adapter = self._create_adapter()
            await _run_in_thread(pooled_adapter.connect)
            if InstrumentedSQLAdapter is not None:
                pooled_adapter = InstrumentedSQLAdapter(pooled_adapter)
            return AsyncSessionWrapper(pooled_adapter)

        # Use pool settings from config if available
        pool_settings = self.config.settings.pool if self.config.settings else None
        min_conn = pool_settings.min_size if pool_settings else 1
        max_conn = pool_settings.max_size if pool_settings else self.config.pool_size
        timeout = pool_settings.timeout if pool_settings else self.config.timeout

        self._async_pool = AsyncConnectionPool(
            create_connection=create_session,
            min_conn=min_conn,
            max_conn=max_conn,
            timeout=timeout,
        )
        self._async_pool_adapter = (
            AsyncPoolTimingAdapter(self._async_pool)
            if AsyncPoolTimingAdapter is not None
            else self._async_pool
        )

        logger.info("Async DB initialized (%s)", self.config.db_type.value)

    def initialize_sync(self) -> None:
        # A dedicated, unpooled adapter for introspection-only calls
        # (get_tables()) that don't go through get_sync_session().
        self._adapter = self._create_adapter()
        self._adapter.connect()

        def create_session():
            # See the comment in initialize() — must be a fresh, independent
            # connection per pooled slot, not the same shared adapter.
            pooled_adapter = self._create_adapter()
            pooled_adapter.connect()
            if InstrumentedSQLAdapter is not None:
                pooled_adapter = InstrumentedSQLAdapter(pooled_adapter)
            return SyncSessionWrapper(pooled_adapter)

        # Use pool settings from config if available
        pool_settings = self.config.settings.pool if self.config.settings else None
        min_conn = pool_settings.min_size if pool_settings else 1
        max_conn = pool_settings.max_size if pool_settings else self.config.pool_size
        timeout = pool_settings.timeout if pool_settings else self.config.timeout

        self._sync_pool = SyncConnectionPool(
            create_connection=create_session,
            min_conn=min_conn,
            max_conn=max_conn,
            timeout=timeout,
        )
        self._sync_pool_adapter = (
            SyncPoolTimingAdapter(self._sync_pool)
            if SyncPoolTimingAdapter is not None
            else self._sync_pool
        )

        logger.info("Sync DB initialized (%s)", self.config.db_type.value)

    def warmup_sync(self, target: int | None = None) -> int:
        """Pre-create sync connections up to `target` (defaults to configured max).

        This method temporarily increases the adaptive sizer current value so the
        pool will create connections, then acquires and immediately releases them
        to populate the pool's available list.

        Returns the number of connections successfully created.
        """
        if not self._sync_pool:
            raise RuntimeError("Sync pool not initialized")

        pool = self._sync_pool
        cfg_max = (
            self.config.settings.pool.max_size
            if getattr(self.config, "settings", None) and self.config.settings.pool
            else self.config.pool_size
        )
        target = min(target or cfg_max, pool._sizer.max)

        created = 0
        # Temporarily set the sizer current so we can create the connections
        with pool._lock:
            original = pool._sizer.current
            pool._sizer.current = max(pool._sizer.current, target)

        conns = []
        try:
            for _ in range(target):
                try:
                    conn = pool.acquire()
                except MaxConnectionsExceeded:
                    break
                else:
                    conns.append(conn)
                    created += 1
        finally:
            for c in conns:
                try:
                    pool.release(c)
                except Exception:
                    pass

        # leave sizer at warmed level (so pool stays warm)
        return created

    async def warmup_async(self, target: int | None = None) -> int:
        """Async variant of warmup_sync: pre-create async connections up to `target`.

        Returns the number of connections successfully created.
        """
        if not self._async_pool:
            raise RuntimeError("Async pool not initialized")

        pool = self._async_pool
        cfg_max = (
            self.config.settings.pool.max_size
            if getattr(self.config, "settings", None) and self.config.settings.pool
            else self.config.pool_size
        )
        target = min(target or cfg_max, pool._sizer.max)

        created = 0

        # Temporarily bump sizer to allow creations
        async with pool._lock:
            original = pool._sizer.current
            pool._sizer.current = max(pool._sizer.current, target)

        conns = []
        try:
            for _ in range(target):
                try:
                    conn = await pool.acquire()
                except MaxConnectionsExceeded:
                    break
                else:
                    conns.append(conn)
                    created += 1
        finally:
            for c in conns:
                try:
                    await pool.release(c)
                except Exception:
                    pass

        return created

    # ------------------ session context ------------------

    @asynccontextmanager
    async def get_async_session(self):
        if not self._async_pool:
            raise RuntimeError("Async pool not initialized")

        # Instrument pool acquire
        if POOL_REQUESTS is not None:
            try:
                POOL_REQUESTS.inc()
            except Exception:
                logger.debug("Failed to increment POOL_REQUESTS", exc_info=True)

        acquire_start = time.perf_counter()
        try:
            if _TRACER:
                with _TRACER.start_as_current_span("db.acquire"):
                    session = await self._async_pool_adapter.acquire()
            else:
                session = await self._async_pool_adapter.acquire()
        except MaxConnectionsExceeded:
            if POOL_TIMEOUTS is not None:
                try:
                    POOL_TIMEOUTS.inc()
                except Exception:
                    logger.debug("Failed to increment POOL_TIMEOUTS", exc_info=True)
            raise
        finally:
            wait_time = time.perf_counter() - acquire_start
            if POOL_WAIT is not None:
                try:
                    POOL_WAIT.observe(wait_time)
                except Exception:
                    logger.debug("Failed to observe POOL_WAIT", exc_info=True)

        # Mark active
        if POOL_ACTIVE is not None:
            try:
                POOL_ACTIVE.inc()
            except Exception:
                logger.debug("Failed to increment POOL_ACTIVE", exc_info=True)

        try:
            yield session
        except Exception:
            await session.rollback()
            raise
        finally:
            # Release and mark inactive
            try:
                await self._async_pool_adapter.release(session)
            finally:
                if POOL_ACTIVE is not None:
                    try:
                        POOL_ACTIVE.dec()
                    except Exception:
                        logger.debug("Failed to decrement POOL_ACTIVE", exc_info=True)

    @contextmanager
    def get_sync_session(self):
        if not self._sync_pool:
            raise RuntimeError("Sync pool not initialized")

        # Instrument pool acquire
        if POOL_REQUESTS is not None:
            try:
                POOL_REQUESTS.inc()
            except Exception:
                logger.debug("Failed to increment POOL_REQUESTS", exc_info=True)

        acquire_start = time.perf_counter()
        try:
            if _TRACER:
                with _TRACER.start_as_current_span("db.acquire"):
                    session = self._sync_pool_adapter.acquire()
            else:
                session = self._sync_pool_adapter.acquire()
        except MaxConnectionsExceeded:
            if POOL_TIMEOUTS is not None:
                try:
                    POOL_TIMEOUTS.inc()
                except Exception:
                    logger.debug("Failed to increment POOL_TIMEOUTS", exc_info=True)
            raise
        finally:
            wait_time = time.perf_counter() - acquire_start
            if POOL_WAIT is not None:
                try:
                    POOL_WAIT.observe(wait_time)
                except Exception:
                    logger.debug("Failed to observe POOL_WAIT", exc_info=True)

        # Mark active
        if POOL_ACTIVE is not None:
            try:
                POOL_ACTIVE.inc()
            except Exception:
                logger.debug("Failed to increment POOL_ACTIVE", exc_info=True)

        try:
            yield session
        except Exception:
            session.rollback()
            raise
        finally:
            try:
                self._sync_pool_adapter.release(session)
            finally:
                if POOL_ACTIVE is not None:
                    try:
                        POOL_ACTIVE.dec()
                    except Exception:
                        logger.debug("Failed to decrement POOL_ACTIVE", exc_info=True)

    # ------------------ instrumented transactions (opt-in) ------------------

    def get_async_transaction(self):
        """Return an instrumented transaction over the async pool.

        Acquire (`POOL_ACQUIRE`) → `BEGIN` (`TRANSACTION_BEGIN`) → yield
        the connection → `COMMIT`/`ROLLBACK`
        (`TRANSACTION_COMMIT`/`TRANSACTION_ROLLBACK`) → release
        (`POOL_RELEASE`), each step timed when a profiler is bound — see
        `core.performance.adapters.transactions.instrumented_async_transaction`
        and `docs/performance/adapters.md`. Opt-in and not called by any
        route in this codebase yet (neither is the uninstrumented
        `core.db.transactions.async_transaction` it wraps); available for
        any write path that wants explicit transactional semantics with
        full observability, e.g.:

            async with db_session.get_async_transaction() as conn:
                await conn.execute("INSERT INTO ...", params)
        """
        if not self._async_pool_adapter:
            raise RuntimeError("Async pool not initialized")
        if instrumented_async_transaction is None:
            raise RuntimeError(
                "core.performance is not installed; instrumented transactions "
                "are unavailable"
            )
        return instrumented_async_transaction(self._async_pool_adapter)

    def get_sync_transaction(self):
        """Sync counterpart of `get_async_transaction`; see its docstring."""
        if not self._sync_pool_adapter:
            raise RuntimeError("Sync pool not initialized")
        if instrumented_transaction is None:
            raise RuntimeError(
                "core.performance is not installed; instrumented transactions "
                "are unavailable"
            )
        return instrumented_transaction(self._sync_pool_adapter)

    # ------------------ health & metadata ------------------

    async def health_check(self) -> bool:
        try:
            async with self.get_async_session() as s:
                await s.execute("SELECT 1")
            return True
        except Exception:
            logger.warning("Async health check failed", exc_info=True)
            return False

    def health_check_sync(self) -> bool:
        try:
            with self.get_sync_session() as s:
                s.execute("SELECT 1")
            return True
        except Exception:
            logger.warning("Sync health check failed", exc_info=True)
            return False

    async def get_tables(self) -> list[str]:
        if not self._adapter:
            raise RuntimeError("Adapter not initialized")
        return await _run_in_thread(self._adapter.get_tables)

    # ------------------ shutdown ------------------

    async def close(self) -> None:
        if self._async_pool:
            await self._async_pool.close_all()
        if self._adapter:
            await _run_in_thread(self._adapter.disconnect)
        self._async_pool = None
        logger.info("Async DB closed")

    def close_sync(self) -> None:
        if self._sync_pool:
            self._sync_pool.close_all()
        if self._adapter:
            self._adapter.disconnect()
        self._sync_pool = None
        logger.info("Sync DB closed")
