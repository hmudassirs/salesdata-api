"""Dedicated, bounded thread pools for blocking I/O -- one per workload.

Why this exists (roadmap rule #5: "Do not use one oversized global
thread pool for unrelated workloads."):

Before this module, every blocking call in the process -- DuckDB
warehouse queries, SQLite service-db reads/writes (auth lookups, query
cache reads/writes, logging), and fire-and-forget background writes --
all went through `asyncio.to_thread`, which dispatches into the event
loop's single *default* executor. That means a burst of slow DuckDB
queries can starve API-key validation, or a wave of cache-hit access-
stat writes can delay someone else's actual query execution, purely
because they're all fighting over the same finite thread pool with no
relationship to each other's workload.

This module instead gives each workload its own bounded
`ThreadPoolExecutor`:

    - `db_executor`: DuckDB warehouse reads/writes (the main query path)
    - `service_executor`: the SQLite service database (auth, API keys,
      query result cache reads/writes)
    - `background_executor`: fire-and-forget work that must never
      compete with request-serving threads for a slot -- cache
      persistence after a miss, access-stat bookkeeping on a cache hit,
      observability flushes

Sizing: each executor's `max_workers` should track the *matching*
connection pool's `max_size` (there's no point in more threads than
there are connections to use them with), plus a little headroom for
in-flight bookkeeping. `configure_executors()` is called once at
startup with the real pool sizes; the defaults here exist only so
importing this module without configuring it still works (e.g. in
tests).
"""

from __future__ import annotations

import asyncio
import contextvars
import functools
import threading
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable, TypedDict

from core.concurrency.cpu import recommended_sizing


class ExecutorMetrics(TypedDict):
    name: str
    max_workers: int
    active: int
    approx_queue_depth: int


class _TrackedExecutor:
    """Thin wrapper around ThreadPoolExecutor that tracks how many
    submitted jobs are currently in flight, so `metrics()` can report
    executor-wait pressure the same way the DB pool reports its own
    (see roadmap section 4.4: `db_executor_wait`)."""

    def __init__(self, name: str, max_workers: int):
        self._name = name
        self._max_workers = max_workers
        self._executor = ThreadPoolExecutor(
            max_workers=max_workers, thread_name_prefix=f"{name}-worker"
        )
        self._active = 0
        self._lock = threading.Lock()

    def resize(self, max_workers: int) -> None:
        """Replace the underlying executor with a differently-sized one.
        Only safe to call at startup before any work has been submitted."""
        self._max_workers = max_workers
        self._executor = ThreadPoolExecutor(
            max_workers=max_workers, thread_name_prefix=f"{self._name}-worker"
        )

    async def run(self, fn: Callable, *args: Any, **kwargs: Any) -> Any:
        loop = asyncio.get_running_loop()
        # `loop.run_in_executor` does NOT propagate contextvars into the
        # worker thread the way `asyncio.to_thread` does -- it just
        # submits a plain callable to the pool. Without this, anything
        # relying on a contextvar set in the calling coroutine (e.g.
        # `core.performance.context.bind_profiler`, which
        # `InstrumentedSQLAdapter` reads via `get_current_profiler()`)
        # silently sees no context in the worker thread, and every
        # `execute`/`fetch_one`/`fetch_all` call goes untimed -- its
        # cost still happens, it just never shows up as its own
        # `sql_execute`/`sql_fetch` stage and gets absorbed into the
        # caller's outer stage instead. Running `fn` through a copy of
        # the current context restores that propagation explicitly.
        ctx = contextvars.copy_context()
        call = functools.partial(ctx.run, fn, *args, **kwargs)
        with self._lock:
            self._active += 1
        try:
            return await loop.run_in_executor(self._executor, call)
        finally:
            with self._lock:
                self._active -= 1

    def metrics(self) -> ExecutorMetrics:
        # `_work_queue.qsize()` is a private implementation detail of
        # ThreadPoolExecutor, but it's the only way to see queued-not-
        # yet-running jobs without maintaining a parallel counter that
        # could drift; guarded with getattr so this degrades gracefully
        # if a future Python version renames/removes it.
        queue = getattr(self._executor, "_work_queue", None)
        approx_queue_depth = queue.qsize() if queue is not None else 0
        with self._lock:
            active = self._active
        return {
            "name": self._name,
            "max_workers": self._max_workers,
            "active": active,
            "approx_queue_depth": approx_queue_depth,
        }

    def shutdown(self, wait: bool = True) -> None:
        self._executor.shutdown(wait=wait)


# Defaults chosen from the host's actual CPU count (core/concurrency/
# cpu.py) rather than flat constants, so importing this module without
# calling configure_executors() first (e.g. in a test, or before
# run_api.py's startup sequencing runs) still gets sane sizing instead
# of numbers picked for one specific machine. Production sizing should
# still come from configure_executors() at startup, tied to the real
# connection pool sizes -- this is only the fallback.
_default_sizing = recommended_sizing()
_db = _TrackedExecutor("db", max_workers=_default_sizing.db_executor_workers)
_service = _TrackedExecutor(
    "service", max_workers=_default_sizing.service_executor_workers
)
_background = _TrackedExecutor(
    "background", max_workers=_default_sizing.background_executor_workers
)


def configure_executors(
    *,
    db_workers: int | None = None,
    service_workers: int | None = None,
    background_workers: int | None = None,
) -> None:
    """(Re)size the shared executors. Call once at startup, before the
    first request, with sizes derived from the real connection pool
    configuration -- e.g. `db_workers = warehouse_pool.max_size + 2`.
    Safe to call more than once (each call replaces the pool outright),
    but only when no work is currently in flight against the old one."""
    if db_workers is not None:
        _db.resize(db_workers)
    if service_workers is not None:
        _service.resize(service_workers)
    if background_workers is not None:
        _background.resize(background_workers)


async def run_in_db_executor(fn: Callable, *args: Any, **kwargs: Any) -> Any:
    """Run a blocking DuckDB warehouse call off the event loop thread."""
    return await _db.run(fn, *args, **kwargs)


async def run_in_service_executor(fn: Callable, *args: Any, **kwargs: Any) -> Any:
    """Run a blocking SQLite service-db call (auth, cache reads/writes)
    off the event loop thread, isolated from warehouse query load."""
    return await _service.run(fn, *args, **kwargs)


async def run_in_background(fn: Callable, *args: Any, **kwargs: Any) -> Any:
    """Run fire-and-forget work (cache persistence, access-stat writes,
    telemetry) on the background executor, isolated from both request-
    serving pools so it can never delay a request that's actually being
    waited on."""
    return await _background.run(fn, *args, **kwargs)


def db_executor() -> _TrackedExecutor:
    return _db


def service_executor() -> _TrackedExecutor:
    return _service


def background_executor() -> _TrackedExecutor:
    return _background


def all_executor_metrics() -> dict[str, ExecutorMetrics]:
    return {
        "db": _db.metrics(),
        "service": _service.metrics(),
        "background": _background.metrics(),
    }


def shutdown_all_executors(wait: bool = True) -> None:
    """Shut down all three executors. Call once at process shutdown,
    after anything that might still submit work to them (the cache
    persistence queue, in particular) has already stopped and drained
    -- see core.app.lifespan's step ordering."""
    _db.shutdown(wait=wait)
    _service.shutdown(wait=wait)
    _background.shutdown(wait=wait)


# asyncio.create_task()'s result must be kept referenced somewhere, or the
# task can be garbage-collected before it finishes running -- a well-known
# asyncio footgun. This module-level set exists purely to hold those
# references until each task completes, then discards itself. Centralized
# here (rather than duplicated per-caller, as it was in the query route)
# so every fire-and-forget background job in the app -- cache writes,
# access-stat bookkeeping, telemetry -- shares one implementation.
_background_tasks: set = set()


def fire_and_forget(coro) -> None:
    """Schedule `coro` to run in the background without the caller
    waiting for it. Any exception is logged, not raised -- by
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
