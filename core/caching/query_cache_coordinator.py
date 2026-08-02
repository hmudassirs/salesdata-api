"""L1 (in-process) cache + single-flight coalescing in front of the
existing L2 (SQLite) query result cache.

Why this exists (roadmap sections 3 and 6): before this module, every
single request -- including N *identical* concurrent requests for the
same not-yet-cached query -- independently hit the L2 cache (a
SQLite read, plus a synchronous access-stat write on every hit) and,
on a miss, independently executed the same query against the
warehouse connection pool. Under a burst of duplicate traffic (the
exact shape of the load test: 500 concurrent identical `SELECT 1`s)
that means 500 SQLite reads and up to 500 redundant warehouse
executions competing for a pool of 10 connections, when one execution
would have satisfied all 500 callers.

This coordinator fixes both problems:

    - L1: a small in-process TTL/LRU cache (`core.db.cache.HybridQueryCache`)
      checked first, with no I/O at all on a hit -- not even a thread
      hop.
    - Single-flight: if N callers ask for the same cache key at the
      same time and it isn't in L1, only the first ("leader") actually
      checks L2 / executes the query; the rest ("followers") await the
      leader's in-flight `asyncio.Future` and get the same result
      without touching the database at all.

Cache-hit traffic never triggers a synchronous write: an L2 hit's
access-stat bookkeeping is scheduled via the bounded cache-persistence
queue (`core.caching.persistence_queue`) onto the background executor,
not awaited inline (roadmap rule #3) and not an unbounded
fire-and-forget task (roadmap Phase 10).
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Dict, Optional

from core.caching.persistence_queue import submit_persist_job
from core.caching.query_result_cache import QueryResultCache
from core.concurrency.executors import run_in_background, run_in_service_executor
from core.db.cache import HybridQueryCache
from core.db.logger import get_logger

logger = get_logger(__name__)

# L1 sizing: bounded so a long-running process with many distinct
# queries can't grow this cache without limit. TTL matches the L2
# default (see QueryResultCache.cache_result's ttl_seconds=3600) so L1
# never serves something L2 would already consider stale.
_L1_MAX_ENTRIES = 2000
_L1_DEFAULT_TTL_SECONDS = 3600


@dataclass
class CacheLookupResult:
    data: list
    source: str  # "l1" | "l2" | "miss"

    @property
    def cached(self) -> bool:
        return self.source in ("l1", "l2")


class QueryCacheCoordinator:
    """Coordinates L1 lookups, single-flight execution, and L2 fallback
    for cacheable (SELECT) query results."""

    def __init__(
        self,
        l2_cache: QueryResultCache,
        l1_max_size: int = _L1_MAX_ENTRIES,
        l1_ttl_seconds: int = _L1_DEFAULT_TTL_SECONDS,
    ):
        self._l2 = l2_cache
        self._l1 = HybridQueryCache(max_size=l1_max_size, default_ttl=l1_ttl_seconds)
        # HybridQueryCache.put() forwards `ttl` straight to CacheEntry and
        # only treats `None` as "no expiry" -- it does NOT fall back to
        # the `default_ttl` given to its constructor when `ttl` is
        # omitted. So every put() below passes this explicitly, or
        # entries would never expire from L1 regardless of L2's TTL.
        self._l1_ttl_seconds = l1_ttl_seconds
        # Guards `_inflight` bookkeeping only (a dict mutation), never
        # held across the actual L2 read or query execution -- same
        # "don't hold the lock across slow I/O" principle as the
        # connection pool fix.
        self._lock = asyncio.Lock()
        self._inflight: Dict[str, "asyncio.Future[CacheLookupResult]"] = {}

    def cache_key(self, sql: str, params: tuple) -> str:
        return self._l2.generate_cache_key(sql, params)

    async def get_or_execute(
        self,
        cache_key: str,
        run_query: Callable[[], Awaitable[list]],
        *,
        on_miss_persist: Optional[Callable[[list], Awaitable[None]]] = None,
    ) -> CacheLookupResult:
        """Resolve `cache_key`, checking L1 then coalescing concurrent
        callers onto a single L2-check-then-execute, and finally falling
        through to `run_query()` on a full miss.

        Args:
            cache_key: pre-computed cache key for this query+params.
            run_query: coroutine function that actually executes the
                query against the warehouse if nothing is cached.
            on_miss_persist: optional coroutine function called (via
                fire-and-forget on the background executor, not
                awaited inline) with the fresh result data when this
                call was the one that actually executed the query --
                lets the caller persist to L2 without adding latency to
                this response.
        """
        cached = self._l1.get(cache_key)
        if cached is not None:
            return CacheLookupResult(data=cached, source="l1")

        loop = asyncio.get_running_loop()
        is_leader = False
        async with self._lock:
            fut = self._inflight.get(cache_key)
            if fut is None:
                fut = loop.create_future()
                self._inflight[cache_key] = fut
                is_leader = True

        if not is_leader:
            # Follower: piggyback on whatever the leader is doing —
            # no L2 read, no query execution, just await their result.
            return await fut

        try:
            result = await self._resolve(cache_key, run_query, on_miss_persist)
            if not fut.done():
                fut.set_result(result)
            return result
        except BaseException as exc:  # noqa: BLE001 -- must propagate to all followers
            if not fut.done():
                fut.set_exception(exc)
            raise
        finally:
            async with self._lock:
                if self._inflight.get(cache_key) is fut:
                    del self._inflight[cache_key]

    async def _resolve(
        self,
        cache_key: str,
        run_query: Callable[[], Awaitable[list]],
        on_miss_persist: Optional[Callable[[list], Awaitable[None]]],
    ) -> CacheLookupResult:
        l2_row = await run_in_service_executor(self._l2.get_cached_result, cache_key)
        if l2_row:
            data = json.loads(l2_row["result_data"])
            self._l1.put(cache_key, data, ttl=self._l1_ttl_seconds)
            # Access-stat bookkeeping is a write; never do it
            # synchronously on the read path (roadmap rule #3), and
            # route it through the *bounded* persistence queue (roadmap
            # Phase 10) rather than an unbounded fire-and-forget task.
            submit_persist_job(run_in_background(self._l2.record_access, cache_key))
            return CacheLookupResult(data=data, source="l2")

        data = await run_query()
        self._l1.put(cache_key, data, ttl=self._l1_ttl_seconds)
        if on_miss_persist is not None and data:
            submit_persist_job(on_miss_persist(data))
        return CacheLookupResult(data=data, source="miss")

    def invalidate(self, cache_key: str) -> None:
        """Drop an entry from L1 only. L2 invalidation is handled
        separately via `QueryResultCache.invalidate_cache()`; call both
        if a query's underlying data has changed."""
        self._l1.invalidate(cache_key)

    async def clear_all(self) -> None:
        """Drop every cached entry, L1 and L2, e.g. after a write
        statement executes (roadmap 16.3). L1 is cleared inline (cheap,
        in-process); L2 goes through the service executor since it's a
        SQLite call."""
        self._l1.clear()
        await run_in_service_executor(self._l2.clear_all)

    async def persist_l2(
        self,
        sql: str,
        data: list,
        params: tuple,
        user_id: str = "",
    ) -> None:
        """Write a freshly-executed result to L2. Callers should invoke
        this via `submit_persist_job`, not await it
        inline on the request path -- the caller already has their
        data; this only helps the *next* request for the same query."""
        await run_in_background(
            self._l2.cache_result, sql, data, params, user_id, "", 0
        )

    def stats(self) -> Dict[str, Any]:
        return {
            "l1": self._l1.stats(),
            "inflight_single_flight_groups": len(self._inflight),
        }
