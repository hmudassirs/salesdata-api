"""Per-process, per-key sliding-window rate limiter.

Used to throttle the two unauthenticated auth endpoints (`/users/login`,
`/users/register`), which would otherwise have no protection against
credential stuffing, password-spraying, or registration spam -- every
other route sits behind API-key/JWT auth, but these two exist
specifically so a caller *without* either can get one.

Deliberately simple: an in-memory dict of deques, no external
dependency. Per-process, like the other in-memory caches in this
codebase (`_USER_CACHE`, `_REVOKED_BEFORE`) -- it does not coordinate
across multiple workers/instances, so a deployment running several
processes behind a load balancer gets a looser effective limit than
configured (roughly `max_attempts * worker_count` per window). Fine as
a baseline defense; front it with a gateway-level limiter for anything
stricter.
"""

from __future__ import annotations

import threading
import time
from collections import defaultdict, deque

_lock = threading.Lock()
_attempts: dict = defaultdict(deque)


def check_and_record(key: str, *, max_attempts: int, window_seconds: float) -> bool:
    """Record one attempt for `key` and report whether it's still within
    the allowed rate.

    Args:
        key: identifies the caller+endpoint being limited, e.g.
            "login:203.0.113.4".
        max_attempts: attempts allowed per `window_seconds`.
        window_seconds: length of the sliding window.

    Returns:
        True if this attempt is allowed, False if `key` has already hit
        `max_attempts` within the current window (caller should reject
        the request, typically with 429).
    """
    now = time.monotonic()
    cutoff = now - window_seconds
    with _lock:
        bucket = _attempts[key]
        while bucket and bucket[0] < cutoff:
            bucket.popleft()
        if len(bucket) >= max_attempts:
            return False
        bucket.append(now)
        return True


def reset(key: str) -> None:
    """Clear recorded attempts for `key`, e.g. after a successful login."""
    with _lock:
        _attempts.pop(key, None)
