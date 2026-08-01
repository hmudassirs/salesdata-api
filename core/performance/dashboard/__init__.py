"""Read-only, optional debug dashboard: `/debug/performance` and
`/debug/request/{id}` (Phase 11). Core profiling remains independent of
this: nothing in `core.performance` outside this package imports it.
"""

from __future__ import annotations

from core.performance.dashboard.fastapi import install_performance_dashboard
from core.performance.dashboard.live import render_dashboard_html
from core.performance.dashboard.summary import (
    DEFAULT_RECENT_REQUESTS_LIMIT,
    build_performance_summary,
)

__all__ = [
    "DEFAULT_RECENT_REQUESTS_LIMIT",
    "build_performance_summary",
    "install_performance_dashboard",
    "render_dashboard_html",
]
