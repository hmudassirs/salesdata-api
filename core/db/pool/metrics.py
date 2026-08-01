from typing import TypedDict


class PoolMetrics(TypedDict):
    max_connections: int
    active_connections: int
    idle_connections: int
    total_requests: int
    pool_hits: int
    pool_misses: int
    total_wait_time: float
    timeout_errors: int
