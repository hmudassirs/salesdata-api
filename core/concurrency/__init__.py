"""Bounded, purpose-specific thread pools for offloading blocking I/O.

See executors.py for why this exists.
"""

from core.concurrency.cpu import ConcurrencySizing, detect_cpu_count, recommended_sizing
from core.concurrency.executors import (
    ExecutorMetrics,
    background_executor,
    configure_executors,
    db_executor,
    fire_and_forget,
    run_in_background,
    run_in_db_executor,
    run_in_service_executor,
    service_executor,
    shutdown_all_executors,
)

__all__ = [
    "ConcurrencySizing",
    "ExecutorMetrics",
    "background_executor",
    "configure_executors",
    "db_executor",
    "detect_cpu_count",
    "fire_and_forget",
    "recommended_sizing",
    "run_in_background",
    "run_in_db_executor",
    "run_in_service_executor",
    "service_executor",
    "shutdown_all_executors",
]
