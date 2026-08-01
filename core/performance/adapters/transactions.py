"""Timing wrappers around `core.db.transactions`'s pool-based transactions.

`transaction`/`async_transaction` in `core.db.transactions` are small
(acquire, `BEGIN`, yield, `COMMIT` or `ROLLBACK`, release) and not built
to be instrumented from the outside: a `contextmanager`'s internal steps
aren't individually observable through its public `__enter__`/`__exit__`.
Rather than change that module, `instrumented_transaction` and
`instrumented_async_transaction` mirror its logic exactly — same
acquire/BEGIN/COMMIT/ROLLBACK/release sequence, same exception
propagation, same "best-effort ROLLBACK" swallow-on-failure behaviour —
with `PerformanceStage.TRANSACTION_BEGIN/COMMIT/ROLLBACK` timers added
around each step. `docs/performance/adapters.md`'s regression tests
assert this against the originals directly.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager, contextmanager
from typing import Any

from core.performance.adapters.pool import _AsyncPoolLike, _SyncPoolLike
from core.performance.context import get_current_profiler
from core.performance.enums import PerformanceStage
from core.performance.types import MetricName


@contextmanager
def instrumented_transaction(pool: _SyncPoolLike) -> Iterator[Any]:
    """Sync equivalent of `core.db.transactions.transaction`, instrumented.

    Behaviourally identical to the original: commits on normal exit,
    best-effort rolls back and re-raises on exception, always releases
    the connection. The only addition is per-step timing.
    """
    profiler = get_current_profiler()
    conn = pool.acquire()
    try:
        if hasattr(conn, "execute"):
            if profiler is None:
                conn.execute("BEGIN")
            else:
                with profiler.stage(
                    PerformanceStage.TRANSACTION_BEGIN, MetricName("transaction_begin")
                ):
                    conn.execute("BEGIN")
        yield conn
        if hasattr(conn, "execute"):
            if profiler is None:
                conn.execute("COMMIT")
            else:
                with profiler.stage(
                    PerformanceStage.TRANSACTION_COMMIT,
                    MetricName("transaction_commit"),
                ):
                    conn.execute("COMMIT")
    except Exception:
        if hasattr(conn, "execute"):
            try:
                if profiler is None:
                    conn.execute("ROLLBACK")
                else:
                    with profiler.stage(
                        PerformanceStage.TRANSACTION_ROLLBACK,
                        MetricName("transaction_rollback"),
                    ):
                        conn.execute("ROLLBACK")
            except Exception:  # matches the original's swallow-on-failure behaviour
                pass
        raise
    finally:
        pool.release(conn)


@asynccontextmanager
async def instrumented_async_transaction(pool: _AsyncPoolLike) -> AsyncIterator[Any]:
    """Async equivalent of `core.db.transactions.async_transaction`, instrumented.

    Behaviourally identical to the original: commits on normal exit,
    best-effort rolls back and re-raises on exception, always releases
    the connection. The only addition is per-step timing.
    """
    profiler = get_current_profiler()
    conn = await pool.acquire()
    try:
        if hasattr(conn, "execute"):
            if profiler is None:
                await conn.execute("BEGIN")
            else:
                with profiler.stage(
                    PerformanceStage.TRANSACTION_BEGIN, MetricName("transaction_begin")
                ):
                    await conn.execute("BEGIN")
        yield conn
        if hasattr(conn, "execute"):
            if profiler is None:
                await conn.execute("COMMIT")
            else:
                with profiler.stage(
                    PerformanceStage.TRANSACTION_COMMIT,
                    MetricName("transaction_commit"),
                ):
                    await conn.execute("COMMIT")
    except Exception:
        if hasattr(conn, "execute"):
            try:
                if profiler is None:
                    await conn.execute("ROLLBACK")
                else:
                    with profiler.stage(
                        PerformanceStage.TRANSACTION_ROLLBACK,
                        MetricName("transaction_rollback"),
                    ):
                        await conn.execute("ROLLBACK")
            except Exception:  # matches the original's swallow-on-failure behaviour
                pass
        raise
    finally:
        await pool.release(conn)
