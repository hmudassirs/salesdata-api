import heapq
import threading
from typing import Any, Dict, List, Tuple

from .adaptive import AdaptiveSizer
from .base import MaxConnectionsExceeded, PoolConnection, now
from .metrics import PoolMetrics


class SyncConnectionPool:
    def __init__(self, create_connection, min_conn=1, max_conn=20, timeout=5):
        self._create = create_connection
        self._lock = threading.RLock()
        self._not_empty = threading.Condition(self._lock)
        self._available: List[Tuple[float, int, Any]] = []
        self._in_use: Dict[int, PoolConnection] = {}
        self._timeout = timeout
        self._counter = 0  # Tiebreaker for heap comparisons

        self._requests = 0
        self._hits = 0
        self._misses = 0
        self._wait_time = 0.0
        self._timeouts = 0

        self._sizer = AdaptiveSizer(min_conn, max_conn)

    def acquire(self):
        start = now()
        self._requests += 1
        deadline = start + self._timeout

        with self._lock:
            while True:
                if self._available:
                    ts, _, conn = heapq.heappop(self._available)
                    self._hits += 1
                    self._wait_time += now() - start
                    self._in_use[id(conn)] = PoolConnection(now(), conn, now())
                    return conn
                elif len(self._in_use) < self._sizer.current:
                    conn = self._create()
                    self._misses += 1
                    self._wait_time += now() - start
                    self._in_use[id(conn)] = PoolConnection(now(), conn, now())
                    return conn

                remaining = deadline - now()
                if remaining <= 0:
                    self._timeouts += 1
                    raise MaxConnectionsExceeded()
                # Every connection is checked out — actually wait for one
                # to be released (up to what's left of `timeout`) instead
                # of failing the instant the pool is momentarily full.
                # release() notifies this condition when a slot frees up.
                self._not_empty.wait(timeout=remaining)

    def release(self, conn):
        with self._lock:
            pc = self._in_use.pop(id(conn), None)
            if pc:
                # Use counter as tiebreaker so heapq doesn't compare wrapper objects
                heapq.heappush(self._available, (pc.created_at, self._counter, conn))
                self._counter += 1
                self._not_empty.notify()

    def close_all(self) -> None:
        """Close every connection this pool ever handed out — idle and
        in-use alike. Nothing previously closed pooled connections at
        all; DatabaseSession.close_sync() only ever disconnected a
        separate standalone adapter, so every connection actually
        created via this pool's create_connection() leaked its
        underlying handle on shutdown."""
        with self._lock:
            conns = [c for _, _, c in self._available]
            conns += [pc.connection for pc in self._in_use.values()]
            self._available.clear()
            self._in_use.clear()

        for conn in conns:
            try:
                if hasattr(conn, "close"):
                    conn.close()
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
