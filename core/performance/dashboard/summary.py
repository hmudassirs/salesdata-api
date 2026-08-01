"""Framework-independent data-shaping for the debug dashboard (Phase 11).

Kept separate from `fastapi.py` so the summary logic is directly
unit-testable without a FastAPI test client, the same split
`request_profiler.py` uses between the profiler itself and
`middleware/fastapi.py`.
"""

from __future__ import annotations

from core.performance.histogram import StreamingHistogram
from core.performance.metric import Counter, Gauge
from core.performance.registry import PerformanceRegistry
from core.performance.request_profiler import RequestProfile

DEFAULT_RECENT_REQUESTS_LIMIT = 20


def build_performance_summary(
    registry: PerformanceRegistry, recent_limit: int = DEFAULT_RECENT_REQUESTS_LIMIT
) -> dict[str, object]:
    """Build the `/debug/performance` response body from `registry`.

    `recent_limit` caps how many of the most-recently-completed
    requests are included (most recent first); the full set remains
    retrievable one at a time via `/debug/request/{id}`.
    """
    history = registry.history()
    recent = history[-recent_limit:] if recent_limit > 0 else []
    return {
        "request_count": len(history),
        "total_requests_recorded": registry.total_requests_recorded,
        "max_request_history": registry.max_request_history,
        "counters": [_counter_dict(c) for c in registry.counters()],
        "gauges": [_gauge_dict(g) for g in registry.gauges()],
        "histograms": [_histogram_dict(h) for h in registry.histograms()],
        "recent_requests": [_history_entry(p) for p in reversed(recent)],
    }


def _counter_dict(counter: Counter) -> dict[str, object]:
    return {"name": counter.name, "tags": dict(counter.tags), "value": counter.value}


def _gauge_dict(gauge: Gauge) -> dict[str, object]:
    return {"name": gauge.name, "tags": dict(gauge.tags), "value": gauge.value}


def _histogram_dict(histogram: StreamingHistogram) -> dict[str, object]:
    return {
        "name": histogram.name,
        "tags": dict(histogram.tags),
        **histogram.snapshot(),
    }


def _history_entry(profile: RequestProfile) -> dict[str, object]:
    return {
        "request_id": profile.request_id,
        "status": profile.status,
        "error": profile.error,
        "duration_ns": profile.duration_ns,
        "tags": dict(profile.tags),
    }
