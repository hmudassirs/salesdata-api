"""Timing wrapper around `core.db.adapters.duckdb.DuckDBAdapter`.

`InstrumentedDuckDBAdapter` wraps a `DuckDBAdapter` instance and times
its SQL-executing and row-fetching methods under `SQL_EXECUTE`/
`SQL_FETCH`, delegating every call — arguments, return value, and
exception — unchanged. Connection-lifecycle methods (`connect`,
`disconnect`) and read-only introspection (`get_tables`, `get_schema`,
`get_database_info`, attribute access such as `.connection`) are not
performance-sensitive per call and are forwarded untimed through
`__getattr__`, so this remains a complete drop-in replacement without
re-declaring the adapter's full surface.

A structural `Protocol` describes the methods this wrapper actually
times, so it works against `DuckDBAdapter`, a test double, or any
future adapter with the same shape — matching `adapters.pool`.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, Protocol, TypeVar

from core.performance.context import get_current_profiler
from core.performance.enums import PerformanceStage
from core.performance.types import MetricName

_T = TypeVar("_T")


class _DuckDBAdapterLike(Protocol):
    """The subset of `DuckDBAdapter`'s interface this adapter times."""

    def execute(self, sql: str, params: list[Any] | None = None) -> Any: ...

    def execute_query(self, sql: str, params: list[Any] | None = None) -> Any: ...

    def fetch_one(
        self, sql: str, params: list[Any] | None = None
    ) -> dict[str, Any] | None: ...

    def fetch_all(
        self, sql: str, params: list[Any] | None = None
    ) -> list[dict[str, Any]]: ...

    def insert(self, table: str, data: dict[str, Any]) -> int: ...

    def update(
        self,
        table: str,
        data: dict[str, Any],
        where: str = "",
        params: list[Any] | None = None,
    ) -> int: ...

    def delete(
        self, table: str, where: str = "", params: list[Any] | None = None
    ) -> int: ...

    def create_table(self, sql: str) -> None: ...

    def drop_table(self, table: str, if_exists: bool = True) -> None: ...


class InstrumentedDuckDBAdapter:
    """Wrap a `DuckDBAdapter`, timing SQL execution and row fetching."""

    __slots__ = ("_adapter",)

    def __init__(self, adapter: _DuckDBAdapterLike) -> None:
        self._adapter = adapter

    def __getattr__(self, name: str) -> Any:
        """Forward anything not explicitly wrapped (untimed) to the adapter."""
        return getattr(self._adapter, name)

    def execute(self, sql: str, params: list[Any] | None = None) -> Any:
        """Execute `sql` and fetch all rows, timed under `SQL_EXECUTE`."""
        return self._timed(
            PerformanceStage.SQL_EXECUTE,
            MetricName("duckdb_execute"),
            lambda: self._adapter.execute(sql, params),
        )

    def execute_query(self, sql: str, params: list[Any] | None = None) -> Any:
        """Execute `sql` and return the raw cursor, timed under `SQL_EXECUTE`."""
        return self._timed(
            PerformanceStage.SQL_EXECUTE,
            MetricName("duckdb_execute_query"),
            lambda: self._adapter.execute_query(sql, params),
        )

    def fetch_one(
        self, sql: str, params: list[Any] | None = None
    ) -> dict[str, Any] | None:
        """Execute `sql` and fetch one row, timed under `SQL_FETCH`."""
        return self._timed(
            PerformanceStage.SQL_FETCH,
            MetricName("duckdb_fetch_one"),
            lambda: self._adapter.fetch_one(sql, params),
        )

    def fetch_all(
        self, sql: str, params: list[Any] | None = None
    ) -> list[dict[str, Any]]:
        """Execute `sql` and fetch all rows, timed under `SQL_FETCH`."""
        return self._timed(
            PerformanceStage.SQL_FETCH,
            MetricName("duckdb_fetch_all"),
            lambda: self._adapter.fetch_all(sql, params),
        )

    def insert(self, table: str, data: dict[str, Any]) -> int:
        """Insert one row into `table`, timed under `SQL_EXECUTE`."""
        return self._timed(
            PerformanceStage.SQL_EXECUTE,
            MetricName("duckdb_insert"),
            lambda: self._adapter.insert(table, data),
        )

    def update(
        self,
        table: str,
        data: dict[str, Any],
        where: str = "",
        params: list[Any] | None = None,
    ) -> int:
        """Update rows in `table`, timed under `SQL_EXECUTE`."""
        return self._timed(
            PerformanceStage.SQL_EXECUTE,
            MetricName("duckdb_update"),
            lambda: self._adapter.update(table, data, where, params),
        )

    def delete(
        self, table: str, where: str = "", params: list[Any] | None = None
    ) -> int:
        """Delete rows from `table`, timed under `SQL_EXECUTE`."""
        return self._timed(
            PerformanceStage.SQL_EXECUTE,
            MetricName("duckdb_delete"),
            lambda: self._adapter.delete(table, where, params),
        )

    def create_table(self, sql: str) -> None:
        """Run a `CREATE TABLE` statement, timed under `SQL_EXECUTE`."""
        self._timed(
            PerformanceStage.SQL_EXECUTE,
            MetricName("duckdb_create_table"),
            lambda: self._adapter.create_table(sql),
        )

    def drop_table(self, table: str, if_exists: bool = True) -> None:
        """Run a `DROP TABLE` statement, timed under `SQL_EXECUTE`."""
        self._timed(
            PerformanceStage.SQL_EXECUTE,
            MetricName("duckdb_drop_table"),
            lambda: self._adapter.drop_table(table, if_exists),
        )

    @staticmethod
    def _timed(stage: PerformanceStage, name: MetricName, call: Callable[[], _T]) -> _T:
        """Run `call()` under `stage`/`name` when a profiler is bound."""
        profiler = get_current_profiler()
        if profiler is None:
            return call()
        with profiler.stage(stage, name):
            return call()
