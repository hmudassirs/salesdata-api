import time
from dataclasses import dataclass
from typing import Any


@dataclass(slots=True)
class PoolConnection:
    created_at: float
    connection: Any
    checked_out_at: float


class MaxConnectionsExceeded(RuntimeError):
    pass


def now() -> float:
    return time.monotonic()
