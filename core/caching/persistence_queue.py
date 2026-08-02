"""Bounded, out-of-band queue for cache persistence writes (roadmap
Phase 10).

Before this module, `QueryCacheCoordinator` scheduled L2 persistence
and access-stat writes via `fire_and_forget()`, which creates an
`asyncio.Task` per call with no upper bound on how many can be pending
at once. Under sustained heavy miss/hit traffic that's unbounded memory
growth -- exactly what the roadmap's Phase 10 calls out. This module
gives cache-persistence work specifically a *bounded* queue with an
explicit backpressure policy (drop, not block) plus the metrics the
roadmap asks for, while still keeping it fully off the request path.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import Awaitable, Optional, TypedDict

from core.db.logger import get_logger

logger = get_logger(__name__)

_DEFAULT_MAXSIZE = 1000
_DEFAULT_WORKERS = 3
_DEFAULT_DRAIN_TIMEOUT_SECONDS = 5.0


class PersistenceMetrics(TypedDict):
    queue_depth: int
    enqueued: int
    completed: int
    failed: int
    dropped: int
    avg_latency_ms: float


@dataclass
class _Job:
    coro: Awaitable
    enqueued_at: float


class CachePersistenceQueue:
    """Single bounded queue + a small pool of consumer tasks. Not a
    generic task queue -- scoped specifically to cache-persistence jobs
    (L2 result writes, access-stat updates) so its bound/backpressure
    policy is one that makes sense for "cache writes are allowed to be
    lossy," not something reused for work that must never be dropped.
    """

    def __init__(
        self, maxsize: int = _DEFAULT_MAXSIZE, workers: int = _DEFAULT_WORKERS
    ):
        self._queue: "asyncio.Queue[_Job]" = asyncio.Queue(maxsize=maxsize)
        self._num_workers = workers
        self._worker_tasks: list[asyncio.Task] = []
        self._accepting = False

        self._enqueued = 0
        self._completed = 0
        self._failed = 0
        self._dropped = 0
        self._total_latency = 0.0

    def start(self) -> None:
        """Begin accepting jobs and spin up consumer workers. Idempotent."""
        if self._accepting:
            return
        self._accepting = True
        self._worker_tasks = [
            asyncio.ensure_future(self._worker_loop())
            for _ in range(self._num_workers)
        ]

    def submit(self, coro: Awaitable) -> bool:
        """Enqueue a persistence job. Non-blocking: if the queue is
        full (sustained persistence backlog) or the queue isn't
        accepting new work (shutting down), the job is dropped rather
        than applying backpressure to the caller -- a lost cache write
        just means the next request for that query re-executes it,
        which is a strictly better failure mode than blocking or
        unbounded memory growth.

        Returns:
            True if enqueued, False if dropped (caller's `coro`, if any
            awaitable it wraps hasn't started, is discarded -- close
            over anything that needs cleanup on drop).
        """
        if not self._accepting:
            self._dropped += 1
            _discard(coro)
            return False
        try:
            self._queue.put_nowait(_Job(coro=coro, enqueued_at=time.monotonic()))
            self._enqueued += 1
            return True
        except asyncio.QueueFull:
            self._dropped += 1
            _discard(coro)
            logger.warning(
                "Cache persistence queue full (maxsize=%d); dropping a write",
                self._queue.maxsize,
            )
            return False

    async def _worker_loop(self) -> None:
        while True:
            job = await self._queue.get()
            try:
                await job.coro
                self._completed += 1
            except asyncio.CancelledError:
                raise
            except Exception:
                self._failed += 1
                logger.warning("Cache persistence job failed", exc_info=True)
            finally:
                self._total_latency += time.monotonic() - job.enqueued_at
                self._queue.task_done()

    async def stop(self, timeout: float = _DEFAULT_DRAIN_TIMEOUT_SECONDS) -> None:
        """Stop accepting new jobs, drain what's queued within
        `timeout`, then cancel any worker still running (roadmap: "stop
        accepting new background jobs; drain queue within timeout" --
        must not hang shutdown indefinitely)."""
        self._accepting = False
        if self._worker_tasks:
            try:
                await asyncio.wait_for(self._queue.join(), timeout=timeout)
            except asyncio.TimeoutError:
                logger.warning(
                    "Cache persistence queue did not drain within %ss "
                    "(%d items still queued); shutting down anyway",
                    timeout,
                    self._queue.qsize(),
                )
            for task in self._worker_tasks:
                task.cancel()
            await asyncio.gather(*self._worker_tasks, return_exceptions=True)
            self._worker_tasks = []

    def metrics(self) -> PersistenceMetrics:
        completed_and_failed = max(1, self._completed + self._failed)
        return {
            "queue_depth": self._queue.qsize(),
            "enqueued": self._enqueued,
            "completed": self._completed,
            "failed": self._failed,
            "dropped": self._dropped,
            "avg_latency_ms": round(
                (self._total_latency / completed_and_failed) * 1000.0, 4
            ),
        }


def _discard(coro: Awaitable) -> None:
    """Best-effort cleanup for a dropped coroutine so Python doesn't
    warn about "coroutine was never awaited" for jobs we intentionally
    never ran."""
    close = getattr(coro, "close", None)
    if callable(close):
        close()


# Process-wide singleton, matching core/concurrency/executors.py's
# shape: one shared queue, configured/started once at startup.
_queue: Optional[CachePersistenceQueue] = None


def get_persistence_queue() -> CachePersistenceQueue:
    global _queue
    if _queue is None:
        _queue = CachePersistenceQueue()
    return _queue


def submit_persist_job(coro: Awaitable) -> bool:
    return get_persistence_queue().submit(coro)


def start_persistence_queue() -> None:
    get_persistence_queue().start()


async def stop_persistence_queue(
    timeout: float = _DEFAULT_DRAIN_TIMEOUT_SECONDS,
) -> None:
    await get_persistence_queue().stop(timeout=timeout)


def persistence_metrics() -> PersistenceMetrics:
    return get_persistence_queue().metrics()
