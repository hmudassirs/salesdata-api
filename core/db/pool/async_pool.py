import asyncio
import heapq
from typing import Any, Dict, List, Tuple

from .adaptive import AdaptiveSizer
from .base import ConnectionState, MaxConnectionsExceeded, PoolConnection, now
from .metrics import PoolMetrics

try:
    from core.performance.histogram import StreamingHistogram
    from core.performance.types import MetricName

    def _new_wait_histogram() -> "StreamingHistogram":
        return StreamingHistogram(name=MetricName("pool_wait_ms"))
except Exception:  # core.performance not installed -- degrade gracefully
    StreamingHistogram = None  # type: ignore[assignment,misc]

    def _new_wait_histogram():
        return None


class AsyncConnectionPool:
    """Async connection pool.

    Correctness properties (see docs/API_PERFORMANCE_LATENCY_REFACTOR_ROADMAP.md
    Phase 3 -- "Pool refactor"):

    - The pool lock is only ever held for O(1) bookkeeping (list/dict
      mutation). Connection *creation* -- which can be slow (opening a
      file, a socket, a subprocess) -- always happens after the lock has
      been released, so one slow connection creation cannot block every
      other coroutine's acquire()/release() bookkeeping.
    - Every waiter created in `acquire()` is removed from `_waiters` on
      every exit path (normal wakeup, timeout, or cancellation), so a
      waiter that gave up never lingers to absorb a future release()'s
      wakeup that was meant for someone still actually waiting.
    """

    def __init__(self, create_connection, min_conn=1, max_conn=20, timeout=5):
        self._create = create_connection
        self._lock = asyncio.Lock()
        self._available: List[Tuple[float, int, Any]] = []
        self._in_use: Dict[int, PoolConnection] = {}
        self._timeout = timeout
        # Ordered list of live waiters (FIFO). A plain list, not
        # asyncio.Queue: Queue.get_nowait() has no way to remove a
        # *specific* waiter (the one that already timed out), only
        # "whichever's first" -- which is exactly the bug that let a
        # dead waiter absorb a release() meant for someone else.
        self._waiters: List[asyncio.Event] = []
        self._counter = 0  # Tiebreaker for heap comparisons

        self._requests = 0
        self._hits = 0
        self._misses = 0
        self._wait_time = 0.0
        self._timeouts = 0
        self._successful_acquires = 0
        self._total_waiters_ever = 0
        self._connection_creation_count = 0
        self._connection_creation_time = 0.0
        # P1-1: connections evicted because release(conn, broken=True)
        # was called -- see core.db.pool.base.is_connection_level_error
        # for the classifier that decides this, and core/db/session.py
        # for where it's applied.
        self._broken_evicted = 0

        self._wait_histogram = _new_wait_histogram()
        # Small rolling window of recent wait times (ms), used by the
        # adaptive sizer for a same-tick "is there pressure right now"
        # signal without needing the full histogram.
        self._recent_waits_ms: List[float] = []
        self._recent_window = 50

        self._sizer = AdaptiveSizer(min_conn, max_conn)

    async def acquire(self):
        start = now()
        self._requests += 1
        waiter: "asyncio.Event | None" = None

        try:
            while True:
                async with self._lock:
                    if self._available:
                        ts, _, conn = heapq.heappop(self._available)
                        self._record_acquire(start, hit=True)
                        self._in_use[id(conn)] = PoolConnection(
                            now(), conn, now(), state=ConnectionState.IN_USE
                        )
                        return conn
                    elif len(self._in_use) < self._sizer.current:
                        # Reserve the slot under the lock (so no other
                        # coroutine also thinks capacity is free), but do
                        # the actual (potentially slow) creation outside
                        # it -- see class docstring. Each reservation
                        # needs its OWN dict key: id(None) would collide
                        # across concurrently-reserving coroutines since
                        # None is a singleton, silently under-counting
                        # in-flight connections.
                        placeholder = object()
                        reserved_key = id(placeholder)
                        self._in_use[reserved_key] = None  # placeholder reservation
                        break

                    # Every connection is checked out and we're at the
                    # size ceiling -- queue up.
                    if waiter is None:
                        waiter = asyncio.Event()
                        self._waiters.append(waiter)
                        self._total_waiters_ever += 1

                # Wait outside the lock for a connection to be released.
                try:
                    await asyncio.wait_for(waiter.wait(), timeout=self._timeout)
                except asyncio.TimeoutError:
                    async with self._lock:
                        if waiter in self._waiters:
                            self._waiters.remove(waiter)
                    self._timeouts += 1
                    self._wait_time += now() - start
                    raise MaxConnectionsExceeded()
                # Woken up -- loop back and try to grab a connection.
                waiter = None
                continue

            # --- creation path: lock already released above ---
            create_start = now()
            try:
                conn = await self._create()
            except BaseException:
                # Any exception here -- including CancelledError, which
                # is a BaseException, not an Exception -- must still give
                # back the reserved slot, or a cancelled acquire()
                # permanently shrinks effective capacity by one.
                async with self._lock:
                    self._in_use.pop(reserved_key, None)
                raise
            creation_elapsed = now() - create_start

            async with self._lock:
                self._in_use.pop(reserved_key, None)
                self._connection_creation_count += 1
                self._connection_creation_time += creation_elapsed
                self._misses += 1
                self._record_acquire(start, hit=False)
                self._in_use[id(conn)] = PoolConnection(
                    now(), conn, now(), state=ConnectionState.IN_USE
                )
            return conn
        except asyncio.CancelledError:
            # Cancellation safety: if we were queued, remove ourselves so
            # a later release() doesn't wake a waiter nobody is awaiting.
            if waiter is not None:
                async with self._lock:
                    if waiter in self._waiters:
                        self._waiters.remove(waiter)
            raise

    def _record_acquire(self, start: float, *, hit: bool) -> None:
        wait = now() - start
        self._wait_time += wait
        self._successful_acquires += 1
        if hit:
            self._hits += 1
        wait_ms = wait * 1000.0
        if self._wait_histogram is not None:
            self._wait_histogram.observe(wait_ms)
        self._recent_waits_ms.append(wait_ms)
        if len(self._recent_waits_ms) > self._recent_window:
            self._recent_waits_ms.pop(0)

    async def release(self, conn, *, broken: bool = False) -> None:
        """Return `conn` to the pool, or -- if `broken=True` -- discard
        it instead (roadmap 6.4/P1-1: "if a connection is broken, remove
        from pool, decrement capacity, create replacement only when
        policy allows"). Capacity is decremented implicitly: popping
        `conn` out of `_in_use` without pushing it into `_available`
        means `total = idle + in_use` (and therefore
        `len(self._in_use) < self._sizer.current` in `acquire()`) drops
        by one, so the very next `acquire()` naturally creates a fresh
        connection instead -- no separate "replacement" bookkeeping
        needed; a waiter is still woken so it re-checks that capacity.

        Call `release(conn, broken=True)` when the code that used `conn`
        caught a connection-level failure (see
        `core.db.pool.base.is_connection_level_error`) -- never for an
        ordinary query error, which says nothing about the connection's
        health and would otherwise waste a perfectly good connection.
        """
        async with self._lock:
            pc = self._in_use.pop(id(conn), None)
            if pc is None:
                return

            if broken:
                pc.state = ConnectionState.BROKEN
                self._broken_evicted += 1
                if hasattr(conn, "close"):
                    try:
                        maybe_close = conn.close()
                        if hasattr(maybe_close, "__await__"):
                            await maybe_close
                    except Exception:
                        pass  # already broken; closing it is best-effort
            else:
                pc.state = ConnectionState.IDLE
                pc.consecutive_errors = 0
                # Use counter as tiebreaker so heapq doesn't compare wrapper objects
                heapq.heappush(self._available, (pc.created_at, self._counter, conn))
                self._counter += 1

            # Wake the oldest live waiter, if any -- whether or not this
            # connection itself went back to `_available`: a broken
            # connection still frees up a capacity slot for a fresh one
            # to be created, which is exactly what a waiter is blocked
            # on. Popping from the front of a real list (not a Queue)
            # means we always wake a waiter that is actually still
            # waiting -- see __init__'s docstring note on why Queue was
            # wrong here.
            if self._waiters:
                self._waiters.pop(0).set()

    async def close_all(self) -> None:
        """Close every connection this pool ever handed out. See
        SyncConnectionPool.close_all() for why this didn't exist before."""
        async with self._lock:
            conns = [c for _, _, c in self._available]
            conns += [pc.connection for pc in self._in_use.values() if pc is not None]
            self._available.clear()
            self._in_use.clear()

        for conn in conns:
            try:
                if hasattr(conn, "close"):
                    await conn.close()
            except Exception:
                pass

    def metrics(self) -> PoolMetrics:
        recent_avg_ms = (
            sum(self._recent_waits_ms) / len(self._recent_waits_ms)
            if self._recent_waits_ms
            else 0.0
        )
        self._sizer.adjust(
            current_waiters=len(self._waiters),
            recent_wait_ms=recent_avg_ms,
        )

        avg_wait_ms = (
            (self._wait_time / max(1, self._successful_acquires + self._timeouts))
            * 1000.0
        )
        p50 = p95 = p99 = None
        if self._wait_histogram is not None and self._wait_histogram.histogram.count:
            snap = self._wait_histogram.snapshot()
            p50, p95, p99 = snap.get("p50"), snap.get("p95"), snap.get("p99")

        current_size = self._sizer.current
        active = len([v for v in self._in_use.values() if v is not None])
        # Reserved-but-not-yet-created slots also count toward "in flight".
        reserved = len(self._in_use) - active

        return {
            "min_connections": self._sizer.min,
            "max_connections": current_size,
            "current_connections": active + len(self._available),
            "active_connections": active,
            "idle_connections": len(self._available),
            "queue_depth": len(self._waiters),
            "current_waiters": len(self._waiters),
            "total_waiters": self._total_waiters_ever,
            "total_requests": self._requests,
            "total_acquires": self._requests,
            "successful_acquires": self._successful_acquires,
            "timed_out_acquires": self._timeouts,
            "pool_hits": self._hits,
            "pool_misses": self._misses,
            "timeout_errors": self._timeouts,
            "total_wait_time": round(self._wait_time, 4),
            "avg_wait_time_ms": round(avg_wait_ms, 4),
            "p50_wait_time_ms": p50,
            "p95_wait_time_ms": p95,
            "p99_wait_time_ms": p99,
            "connection_creation_count": self._connection_creation_count,
            "connection_creation_time_ms": round(
                self._connection_creation_time * 1000.0, 4
            ),
            "broken_connections_evicted": self._broken_evicted,
            "utilization": round(active / max(1, current_size), 4),
            "saturation": round(
                (active + reserved) / max(1, current_size), 4
            ),
        }
