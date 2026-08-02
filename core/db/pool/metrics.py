from typing import Optional, TypedDict


class PoolMetrics(TypedDict):
    # Sizing
    min_connections: int
    max_connections: int
    current_connections: int
    active_connections: int
    idle_connections: int

    # Queueing (Phase 2 additions -- see roadmap section 5)
    queue_depth: int
    current_waiters: int
    total_waiters: int

    # Acquire outcomes
    total_requests: int
    total_acquires: int
    successful_acquires: int
    timed_out_acquires: int
    pool_hits: int
    pool_misses: int
    timeout_errors: int

    # Wait-time distribution
    total_wait_time: float
    avg_wait_time_ms: float
    p50_wait_time_ms: Optional[float]
    p95_wait_time_ms: Optional[float]
    p99_wait_time_ms: Optional[float]

    # Connection creation cost
    connection_creation_count: int
    connection_creation_time_ms: float
    broken_connections_evicted: int

    # Pressure summary
    utilization: float
    saturation: float
