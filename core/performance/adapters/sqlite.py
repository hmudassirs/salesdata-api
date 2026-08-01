"""Timing wrappers around `core.storage.service_db.ServiceDatabase` and
`core.caching.query_result_cache.QueryResultCache`.

Mirrors the shape of `adapters.duckdb`: wrap the existing object behind
an identical interface, delegate every call — arguments, return value,
and exception — unchanged, and only add timing when a profiler is bound
to the current context.

Two seams are covered, matching `docs/PerformancePlan.md` Phase 8 and
the `SQL` / `CACHE_LOOKUP` / `CACHE_STORE` nodes in
`docs/RequestFlow.md`'s trace model:

- `InstrumentedServiceDatabase` wraps `ServiceDatabase`. `execute`,
  `fetch_one`, and `fetch_all` (each of which acquires its own pooled
  connection) are timed under `SQL_EXECUTE`/`SQL_FETCH`. `execute_on`
  and `fetch_one_on` (used against an already-acquired connection
  inside `transaction()`) get the same stages under a distinct metric
  name so the two call shapes stay distinguishable in a trace.
  `transaction()` itself — acquire, yield, commit-or-rollback — is
  timed as one `TRANSACTION_COMMIT` span: `ServiceDatabase.transaction`
  relies on sqlite3's implicit-transaction/`.commit()`/`.rollback()`
  API rather than executing explicit `BEGIN`/`COMMIT`/`ROLLBACK`
  statements (contrast `core.db.transactions.transaction`, which
  `adapters.transactions` times per SQL statement), so there is no
  separate begin/commit boundary to instrument without duplicating
  `ServiceDatabase`'s private connection-acquisition logic. Everything
  else (`connect`, `disconnect`, `metrics`, `create_tables`, the
  API-key/user CRUD helpers, `cleanup_expired_cache`, `get_stats`,
  `initialize_admin_user`) is administrative or one-time-per-process
  rather than per-request and is forwarded untimed through
  `__getattr__`.
- `InstrumentedQueryResultCache` wraps `QueryResultCache`.
  `get_cached_result` is timed under `CACHE_LOOKUP` and `cache_result`
  under `CACHE_STORE` — the two operations on the `/api/query` request
  path per `docs/RequestFlow.md`. `generate_cache_key` (pure, in-memory
  hashing), `invalidate_cache`, and `get_cache_stats` (administrative)
  are forwarded untimed.

Neither `core.storage.service_db` nor `core.caching.query_result_cache`
is modified; adopting either wrapper is opt-in at the call site.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from typing import TYPE_CHECKING, Any, TypeVar

from core.performance.context import get_current_profiler
from core.performance.enums import PerformanceStage
from core.performance.types import MetricName

if TYPE_CHECKING:
    from core.caching.query_result_cache import QueryResultCache
    from core.storage.service_db import ExecuteResult, ServiceDatabase

_T = TypeVar("_T")


class InstrumentedServiceDatabase:
    """Wrap a `ServiceDatabase`, timing its SQL execute/fetch operations."""

    __slots__ = ("_db",)

    def __init__(self, service_db: ServiceDatabase) -> None:
        self._db = service_db

    def __getattr__(self, name: str) -> Any:
        """Forward anything not explicitly wrapped (untimed) to the database."""
        return getattr(self._db, name)

    def execute(self, sql: str, params: tuple[Any, ...] = ()) -> ExecuteResult:
        """Execute a write statement and commit, timed under `SQL_EXECUTE`."""
        return self._timed(
            PerformanceStage.SQL_EXECUTE,
            MetricName("service_db_execute"),
            lambda: self._db.execute(sql, params),
        )

    def fetch_one(self, sql: str, params: tuple[Any, ...] = ()) -> Any:
        """Fetch a single row, timed under `SQL_FETCH`."""
        return self._timed(
            PerformanceStage.SQL_FETCH,
            MetricName("service_db_fetch_one"),
            lambda: self._db.fetch_one(sql, params),
        )

    def fetch_all(self, sql: str, params: tuple[Any, ...] = ()) -> Any:
        """Fetch all rows, timed under `SQL_FETCH`."""
        return self._timed(
            PerformanceStage.SQL_FETCH,
            MetricName("service_db_fetch_all"),
            lambda: self._db.fetch_all(sql, params),
        )

    def execute_on(
        self, adapter: Any, sql: str, params: tuple[Any, ...] = ()
    ) -> ExecuteResult:
        """Run a statement on an already-acquired adapter, timed under `SQL_EXECUTE`.

        For use inside `transaction()`, matching `ServiceDatabase.execute_on`:
        does not acquire a new connection or commit.
        """
        return self._timed(
            PerformanceStage.SQL_EXECUTE,
            MetricName("service_db_execute_on"),
            lambda: self._db.execute_on(adapter, sql, params),
        )

    def fetch_one_on(self, adapter: Any, sql: str, params: tuple[Any, ...] = ()) -> Any:
        """Fetch one row on an already-acquired adapter, timed under `SQL_FETCH`."""
        return self._timed(
            PerformanceStage.SQL_FETCH,
            MetricName("service_db_fetch_one_on"),
            lambda: self._db.fetch_one_on(adapter, sql, params),
        )

    @contextmanager
    def transaction(self) -> Iterator[Any]:
        """Run `ServiceDatabase.transaction()`, timed as one `TRANSACTION_COMMIT` span.

        Yields the same already-acquired adapter as the original, and
        commits on normal exit or rolls back and re-raises on
        exception — identical behaviour to the wrapped method. See the
        module docstring for why this is timed as a single span rather
        than split into begin/commit/rollback stages.
        """
        profiler = get_current_profiler()
        if profiler is None:
            with self._db.transaction() as adapter:
                yield adapter
            return
        with (
            profiler.stage(
                PerformanceStage.TRANSACTION_COMMIT,
                MetricName("service_db_transaction"),
            ),
            self._db.transaction() as adapter,
        ):
            yield adapter

    @staticmethod
    def _timed(stage: PerformanceStage, name: MetricName, call: Callable[[], _T]) -> _T:
        """Run `call()` under `stage`/`name` when a profiler is bound."""
        profiler = get_current_profiler()
        if profiler is None:
            return call()
        with profiler.stage(stage, name):
            return call()


class InstrumentedQueryResultCache:
    """Wrap a `QueryResultCache`, timing lookup/store under cache stages."""

    __slots__ = ("_cache",)

    def __init__(self, cache: QueryResultCache) -> None:
        self._cache = cache

    def __getattr__(self, name: str) -> Any:
        """Forward anything not explicitly wrapped (untimed) to the cache."""
        return getattr(self._cache, name)

    def get_cached_result(self, cache_key: str) -> dict[str, Any] | None:
        """Look up one cache entry, timed under `CACHE_LOOKUP`."""
        return self._timed(
            PerformanceStage.CACHE_LOOKUP,
            MetricName("query_cache_lookup"),
            lambda: self._cache.get_cached_result(cache_key),
        )

    def cache_result(  # noqa: PLR0913, PLR0917
        self,
        query_sql: str,
        result_data: list[dict[str, Any]],
        params: tuple[Any, ...] = (),
        user_id: str = "",
        session_id: str = "",
        execution_time_ms: int = 0,
        ttl_seconds: int = 3600,
    ) -> str:
        """Store one cache entry, timed under `CACHE_STORE`."""
        return self._timed(
            PerformanceStage.CACHE_STORE,
            MetricName("query_cache_store"),
            lambda: self._cache.cache_result(
                query_sql,
                result_data,
                params,
                user_id,
                session_id,
                execution_time_ms,
                ttl_seconds,
            ),
        )

    @staticmethod
    def _timed(stage: PerformanceStage, name: MetricName, call: Callable[[], _T]) -> _T:
        """Run `call()` under `stage`/`name` when a profiler is bound."""
        profiler = get_current_profiler()
        if profiler is None:
            return call()
        with profiler.stage(stage, name):
            return call()
