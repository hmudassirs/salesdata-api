import asyncio
import heapq
from typing import Any, Dict, List, Tuple

from .adaptive import AdaptiveSizer
from .base import MaxConnectionsExceeded, PoolConnection, now
from .metrics import PoolMetrics


class AsyncConnectionPool:
    def __init__(self, create_connection, min_conn=1, max_conn=20, timeout=5):
        self._create = create_connection
        self._lock = asyncio.Lock()
        self._available: List[Tuple[float, int, Any]] = []
        self._in_use: Dict[int, PoolConnection] = {}
        self._timeout = timeout
        self._waiters: asyncio.Queue = asyncio.Queue()  # Queue for waiting tasks
        self._counter = 0  # Tiebreaker for heap comparisons

        self._requests = 0
        self._hits = 0
        self._misses = 0
        self._wait_time = 0.0
        self._timeouts = 0

        self._sizer = AdaptiveSizer(min_conn, max_conn)

    async def acquire(self):
        start = now()
        self._requests += 1

        while True:
            async with self._lock:
                if self._available:
                    ts, _, conn = heapq.heappop(self._available)
                    self._hits += 1
                    self._wait_time += now() - start
                    self._in_use[id(conn)] = PoolConnection(now(), conn, now())
                    return conn
                elif len(self._in_use) < self._sizer.current:
                    conn = await self._create()
                    self._misses += 1
                    self._wait_time += now() - start
                    self._in_use[id(conn)] = PoolConnection(now(), conn, now())
                    return conn

                # Wait for a connection to be released
                waiter = asyncio.Event()
                await self._waiters.put(waiter)

            # Wait outside the lock for a connection to be released
            try:
                await asyncio.wait_for(waiter.wait(), timeout=self._timeout)
            except asyncio.TimeoutError:
                self._timeouts += 1
                raise MaxConnectionsExceeded()

    async def release(self, conn):
        async with self._lock:
            pc = self._in_use.pop(id(conn), None)
            if pc:
                # Use counter as tiebreaker so heapq doesn't compare wrapper objects
                heapq.heappush(self._available, (pc.created_at, self._counter, conn))
                self._counter += 1

                # Notify waiting tasks
                if not self._waiters.empty():
                    try:
                        waiter = self._waiters.get_nowait()
                        waiter.set()
                    except asyncio.QueueEmpty:
                        pass

    async def close_all(self) -> None:
        """Close every connection this pool ever handed out. See
        SyncConnectionPool.close_all() for why this didn't exist before."""
        async with self._lock:
            conns = [c for _, _, c in self._available]
            conns += [pc.connection for pc in self._in_use.values()]
            self._available.clear()
            self._in_use.clear()

        for conn in conns:
            try:
                if hasattr(conn, "close"):
                    await conn.close()
            except Exception:
                pass

    def metrics(self) -> PoolMetrics:
        hit_ratio = self._hits / max(1, self._requests)
        self._sizer.adjust(hit_ratio)

        return {
            "max_connections": self._sizer.current,
            "active_connections": len(self._in_use),
            "idle_connections": len(self._available),
            "total_requests": self._requests,
            "pool_hits": self._hits,
            "pool_misses": self._misses,
            "total_wait_time": round(self._wait_time, 4),
            "timeout_errors": self._timeouts,
        }
