"""Process-wide concurrency gates for query cost classes (roadmap
Phase 14) and the config for result/duration limits (roadmap 13.4).

Mirrors core/concurrency/executors.py's shape deliberately: module-level
singletons sized by `configure_query_limits()` at startup, so every
request shares the same semaphores rather than each `QueryService`
instance creating its own (which would make the limits meaningless --
a fresh semaphore per request never actually contends with anything).
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Dict, TypedDict

from core.concurrency.cpu import recommended_sizing
from core.db.sql_policy import Cost


class QueryLimitsMetrics(TypedDict):
    limit: int
    in_use: int


@dataclass(frozen=True)
class QueryLimits:
    max_result_rows: int = 10_000
    max_result_bytes: int = 10 * 1024 * 1024
    max_query_duration_seconds: float = 30.0


_limits = QueryLimits()

# Fallback defaults if configure_query_limits() is never called (e.g.
# a module imported directly in a test) -- derived from the actual host
# CPU count rather than a flat constant, same reasoning as
# core/concurrency/executors.py's defaults.
_sizing = recommended_sizing()
_semaphores: Dict[Cost, asyncio.Semaphore] = {
    "fast": asyncio.Semaphore(_sizing.fast_query_concurrency),
    "normal": asyncio.Semaphore(_sizing.normal_query_concurrency),
    "expensive": asyncio.Semaphore(_sizing.expensive_query_concurrency),
}
_capacity: Dict[Cost, int] = {
    "fast": _sizing.fast_query_concurrency,
    "normal": _sizing.normal_query_concurrency,
    "expensive": _sizing.expensive_query_concurrency,
}


def configure_query_limits(
    *,
    max_result_rows: int | None = None,
    max_result_bytes: int | None = None,
    max_query_duration_seconds: float | None = None,
    fast_concurrency: int | None = None,
    normal_concurrency: int | None = None,
    expensive_concurrency: int | None = None,
) -> None:
    """(Re)configure result limits and per-class semaphores. Call once
    at startup from AppSettings; safe to call again only when no query
    is currently holding one of the semaphores being replaced."""
    global _limits
    _limits = QueryLimits(
        max_result_rows=max_result_rows
        if max_result_rows is not None
        else _limits.max_result_rows,
        max_result_bytes=max_result_bytes
        if max_result_bytes is not None
        else _limits.max_result_bytes,
        max_query_duration_seconds=max_query_duration_seconds
        if max_query_duration_seconds is not None
        else _limits.max_query_duration_seconds,
    )
    if fast_concurrency is not None:
        _semaphores["fast"] = asyncio.Semaphore(fast_concurrency)
        _capacity["fast"] = fast_concurrency
    if normal_concurrency is not None:
        _semaphores["normal"] = asyncio.Semaphore(normal_concurrency)
        _capacity["normal"] = normal_concurrency
    if expensive_concurrency is not None:
        _semaphores["expensive"] = asyncio.Semaphore(expensive_concurrency)
        _capacity["expensive"] = expensive_concurrency


def get_limits() -> QueryLimits:
    return _limits


def semaphore_for(cost: Cost) -> asyncio.Semaphore:
    return _semaphores[cost]


def query_concurrency_metrics() -> Dict[str, QueryLimitsMetrics]:
    metrics: Dict[str, QueryLimitsMetrics] = {}
    for cost, sem in _semaphores.items():
        limit = _capacity[cost]
        # asyncio.Semaphore has no public "in use" counter; `_value` is
        # the remaining-permits count, so `limit - _value` is in-flight.
        # Guarded getattr in case a future Python release changes the
        # private attribute name -- degrades to "unknown" (0) rather
        # than crashing a health check.
        remaining = getattr(sem, "_value", limit)
        metrics[cost] = {"limit": limit, "in_use": max(0, limit - remaining)}
    return metrics
