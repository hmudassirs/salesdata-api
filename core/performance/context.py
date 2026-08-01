"""Request-local storage for the active profiler and trace.

Uses `contextvars.ContextVar`, which is copy-on-task in asyncio and
thread-local under `ThreadPoolExecutor`/plain threads. This gives every
concurrently running request its own view of "the current profiler"
without any shared mutable state or lock: two requests running at once
never see or contend on each other's `ContextVar` value.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar, Token
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .request_profiler import RequestProfiler

_current_profiler: ContextVar[RequestProfiler | None] = ContextVar(
    "performance_current_profiler", default=None
)


def get_current_profiler() -> RequestProfiler | None:
    """Return the profiler bound to the current task/thread, if any."""
    return _current_profiler.get()


def set_current_profiler(
    profiler: RequestProfiler | None,
) -> Token[RequestProfiler | None]:
    """Bind `profiler` as current and return a token to restore the prior value."""
    return _current_profiler.set(profiler)


def reset_current_profiler(token: Token[RequestProfiler | None]) -> None:
    """Restore the profiler that was current before the matching `set` call."""
    _current_profiler.reset(token)


@contextmanager
def bind_profiler(profiler: RequestProfiler | None) -> Iterator[RequestProfiler | None]:
    """Bind `profiler` as current for the duration of the `with` block.

    A no-op profiler (`None`, or one built with `enabled=False`) can still
    be bound; callers checking `get_current_profiler()` are expected to
    handle `None` and disabled profilers identically, both being no-ops.
    """
    token = set_current_profiler(profiler)
    try:
        yield profiler
    finally:
        reset_current_profiler(token)
