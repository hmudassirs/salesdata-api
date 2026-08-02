"""Adaptive pool sizing based on real contention signals.

Previous behavior (removed): grow the pool whenever the *hit ratio*
(idle-connection reuse rate) dropped below 70%, shrink when it rose
above 95%. That metric is the wrong signal for this problem: hit ratio
falls to zero the instant the pool is momentarily saturated *and* stays
low for as long as load is high, regardless of whether anyone is
actually waiting -- it can't distinguish "clients are queueing for a
connection" (grow) from "every request happens to need a brand-new
connection but nobody waited" (no action needed). It also can't shrink
back down safely, because a saturated pool trivially keeps a hit ratio
of 0%.

New behavior: size the pool from the two things that actually indicate
contention --

    - queue pressure: how many callers are *currently* waiting for a
      connection (``current_waiters``)
    - wait latency: how long recent acquires actually waited
      (``recent_wait_ms``, a short rolling window average -- see
      `AsyncConnectionPool`/`SyncConnectionPool`)

Growth is immediate (a queue forming right now should be relieved right
now, bounded by `max`). Shrinkage requires sustained idleness across
several consecutive observations (`_COOLDOWN_CHECKS`) so the pool
doesn't thrash size on every request -- this is the hysteresis/cooldown
the roadmap calls for. Bounds (`min`/`max`) are hard limits set by
configuration, benchmarked ahead of time; this class never sizes itself
based on how many HTTP requests happen to be in flight.
"""

from dataclasses import dataclass, field


# Sustained-idle checks required before shrinking by one connection.
# Requires several consecutive "no pressure" observations in a row, so
# a single quiet moment right after a burst doesn't immediately give
# back capacity that's likely to be needed again shortly.
_COOLDOWN_CHECKS = 5

# A recent average acquire wait above this is treated as real
# contention worth growing for, even if no one happens to be waiting
# at the exact instant `adjust()` is called.
_WAIT_PRESSURE_THRESHOLD_MS = 5.0


@dataclass
class AdaptiveSizer:
    """Dynamically grows or shrinks pool size based on measured queue
    pressure and acquire-wait latency, never on hit ratio."""

    min: int
    max: int
    current: int = field(init=False)
    _idle_streak: int = field(default=0, init=False, repr=False)

    def __post_init__(self) -> None:
        self.current = self.min

    def adjust(
        self,
        *,
        current_waiters: int = 0,
        recent_wait_ms: float = 0.0,
    ) -> int:
        """Recompute `current` from live contention signals.

        Args:
            current_waiters: number of callers blocked in `acquire()`
                right now, waiting for a connection.
            recent_wait_ms: average acquire wait time over a recent
                rolling window, in milliseconds.

        Returns:
            The (possibly unchanged) new `current` size.
        """
        under_pressure = (
            current_waiters > 0 or recent_wait_ms > _WAIT_PRESSURE_THRESHOLD_MS
        )

        if under_pressure:
            self._idle_streak = 0
            if self.current < self.max:
                self.current += 1
        else:
            self._idle_streak += 1
            if self._idle_streak >= _COOLDOWN_CHECKS and self.current > self.min:
                self.current -= 1
                self._idle_streak = 0

        return self.current
