"""Shared protocol for optional, process-wide resource collectors.

Per `docs/PerformancePlan.md` Phase 9, these collectors are a different
role from the `Collector` `Protocol` in `registry.py`: that one observes
a just-completed *request* (`RequestProfile`); a `ResourceCollector`
samples *process-wide* state (heap size, GC generation counts, thread
count, ...) that has no single owning request. Each collector's
`collect()` returns a plain, immutable list of `MetricPoint`s — a
snapshot at the moment it is called — so callers decide how and how
often to invoke it (a background scheduler, a debug-dashboard request,
a benchmark, or a test) rather than the collector owning a timer of its
own.

Every collector degrades to an empty list rather than raising when the
underlying OS/runtime facility it reads is unavailable (e.g.
`resource` on non-POSIX platforms, no running asyncio loop): a resource
collector must never break the request or process it is observing.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from core.performance.metric import MetricPoint


@runtime_checkable
class ResourceCollector(Protocol):
    """Something that can sample current process-wide resource state."""

    name: str

    def collect(self) -> list[MetricPoint]:
        """Return a snapshot of gauge points, or `[]` if unavailable."""
