"""Timing wrappers around `core.db.pool`'s async and sync connection pools.

`AsyncConnectionPool` and `SyncConnectionPool` are wrapped rather than
modified: `AsyncPoolTimingAdapter`/`SyncPoolTimingAdapter` expose the
same `acquire`/`release`/`close_all`/`metrics` surface and delegate every
call unchanged, so a call site can swap `pool` for
`AsyncPoolTimingAdapter(pool)` with no other code change and identical
behaviour (including `MaxConnectionsExceeded` on timeout).

Structural `Protocol`s are used instead of importing the concrete pool
classes so the adapter works with either pool, a test double, or any
future pool implementation with the same shape.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Protocol

from core.performance.context import get_current_profiler
from core.performance.enums import PerformanceStage
from core.performance.types import MetricName


class _AsyncPoolLike(Protocol):
    """The subset of `AsyncConnectionPool`'s interface this adapter uses."""

    async def acquire(self) -> Any: ...

    async def release(self, conn: Any, *, broken: bool = False) -> None: ...

    async def close_all(self) -> None: ...

    def metrics(self) -> Mapping[str, Any]: ...


class _SyncPoolLike(Protocol):
    """The subset of `SyncConnectionPool`'s interface this adapter uses."""

    def acquire(self) -> Any: ...

    def release(self, conn: Any, *, broken: bool = False) -> None: ...

    def close_all(self) -> None: ...

    def metrics(self) -> Mapping[str, Any]: ...


def _emit_pool_gauges(profiler: Any, metrics: Mapping[str, Any]) -> None:
    """Publish the pool's own reported active/idle counts as gauges.

    Reads only the two fields every pool implementation in this codebase
    already computes in `metrics()`; no pool-internal state is touched.
    """
    active = metrics.get("active_connections")
    idle = metrics.get("idle_connections")
    if active is not None:
        profiler.gauge(MetricName("pool_active_connections"), active)
    if idle is not None:
        profiler.gauge(MetricName("pool_idle_connections"), idle)


class AsyncPoolTimingAdapter:
    """Wrap an async connection pool with acquire/release timing."""

    __slots__ = ("_pool",)

    def __init__(self, pool: _AsyncPoolLike) -> None:
        self._pool = pool

    async def acquire(self) -> Any:
        """Check out a connection, timing the wait under `POOL_ACQUIRE`."""
        profiler = get_current_profiler()
        if profiler is None:
            return await self._pool.acquire()
        with profiler.stage(
            PerformanceStage.POOL_ACQUIRE, MetricName("pool_acquire")
        ):
            conn = await self._pool.acquire()
        _emit_pool_gauges(profiler, self._pool.metrics())
        return conn

    async def release(self, conn: Any, *, broken: bool = False) -> None:
        """Return a connection to the pool, timing it under `POOL_RELEASE`."""
        profiler = get_current_profiler()
        if profiler is None:
            await self._pool.release(conn, broken=broken)
            return
        with profiler.stage(
            PerformanceStage.POOL_RELEASE, MetricName("pool_release")
        ):
            await self._pool.release(conn, broken=broken)
        _emit_pool_gauges(profiler, self._pool.metrics())

    async def close_all(self) -> None:
        """Delegate to the wrapped pool's `close_all`, untimed."""
        await self._pool.close_all()

    def metrics(self) -> Mapping[str, Any]:
        """Delegate to the wrapped pool's `metrics`, untimed."""
        return self._pool.metrics()


class SyncPoolTimingAdapter:
    """Wrap a sync connection pool with acquire/release timing."""

    __slots__ = ("_pool",)

    def __init__(self, pool: _SyncPoolLike) -> None:
        self._pool = pool

    def acquire(self) -> Any:
        """Check out a connection, timing the wait under `POOL_ACQUIRE`."""
        profiler = get_current_profiler()
        if profiler is None:
            return self._pool.acquire()
        with profiler.stage(
            PerformanceStage.POOL_ACQUIRE, MetricName("pool_acquire")
        ):
            conn = self._pool.acquire()
        _emit_pool_gauges(profiler, self._pool.metrics())
        return conn

    def release(self, conn: Any, *, broken: bool = False) -> None:
        """Return a connection to the pool, timing it under `POOL_RELEASE`."""
        profiler = get_current_profiler()
        if profiler is None:
            self._pool.release(conn, broken=broken)
            return
        with profiler.stage(
            PerformanceStage.POOL_RELEASE, MetricName("pool_release")
        ):
            self._pool.release(conn, broken=broken)
        _emit_pool_gauges(profiler, self._pool.metrics())

    def close_all(self) -> None:
        """Delegate to the wrapped pool's `close_all`, untimed."""
        self._pool.close_all()

    def metrics(self) -> Mapping[str, Any]:
        """Delegate to the wrapped pool's `metrics`, untimed."""
        return self._pool.metrics()
