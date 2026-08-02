"""Adaptive request sampling (roadmap Phase 11 / P1-4).

`PerformanceConfig.should_sample()` (core/performance/config.py) makes a
fixed-probability decision *before* the request runs, since a detailed
profiler has to be opened (or not) up front. That's fine for a steady
target rate, but the roadmap also asks for two things a fixed rate
can't do on its own:

    1. "Adaptive" sampling -- hold to a target *volume* of samples/sec
       rather than a fixed percentage, so the absolute number of traces
       produced doesn't scale linearly (and unboundedly) with traffic.
    2. "Always keep error/slow/timeout requests high priority" -- a
       request's outcome isn't known until *after* it runs, which is
       fundamentally incompatible with a pre-request sampling decision
       for that *specific* request's own detailed trace. What this
       module does instead, honestly: it always cheaply measures every
       request's duration and status (that part is unconditional, not
       sampled), and when an unsampled request turns out to be an
       error or slow, it (a) logs that occurrence directly so it's
       never silently invisible, and (b) escalates the sampling rate
       for a window afterward, so subsequent requests of the same
       shape *do* get a full trace while the problem is ongoing.

This is a "shadow timing + escalation" design, not full tail-based
sampling (which would require buffering every request's complete trace
and deciding to keep/discard after the fact -- expensive, and the exact
"pay full profiling cost for every request" cost this whole roadmap
exists to avoid). It trades perfect retroactive capture for the
much cheaper unconditional signal of "did anything go wrong", which is
enough to know an incident is happening and start capturing detail for
it immediately.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)


@dataclass
class AdaptiveSamplerConfig:
    target_samples_per_second: float = 5.0
    min_rate_percent: float = 1.0
    max_rate_percent: float = 100.0
    adjustment_interval_seconds: float = 5.0
    # A request slower than this is always treated as "bad" for
    # escalation purposes, sampled or not.
    slow_request_threshold_seconds: float = 2.0
    # How long an escalation (forced full-rate sampling) lasts after
    # the triggering error/slow request, before decaying back to the
    # target-rate-adjusted value.
    escalation_seconds: float = 30.0


class AdaptiveSampler:
    """Thread-safe (the FastAPI middleware may run request handling
    across multiple event-loop-driven coroutines, but only one thread
    per worker process in this codebase's deployment model -- the lock
    is cheap insurance, not load-bearing for correctness under real
    concurrency)."""

    def __init__(self, config: AdaptiveSamplerConfig | None = None):
        self._config = config or AdaptiveSamplerConfig()
        self._lock = threading.Lock()
        self._rate_percent = self._config.max_rate_percent
        self._window_start = time.monotonic()
        self._window_sampled = 0
        self._window_requests = 0
        self._escalated_until = 0.0

        # Lifetime counters, for metrics/observability only.
        self._total_requests = 0
        self._total_sampled = 0
        self._total_escalations = 0
        self._total_bad_outcomes = 0

    def should_sample(self) -> bool:
        """Decide whether to open a full profiler for the request about
        to run. Called once, before the request executes."""
        import random

        with self._lock:
            self._total_requests += 1
            self._window_requests += 1
            self._maybe_adjust_locked()

            escalated = time.monotonic() < self._escalated_until
            sampled = escalated or (random.uniform(0, 100) < self._rate_percent)

            if sampled:
                self._window_sampled += 1
                self._total_sampled += 1
            return sampled

    def record_outcome(
        self, *, status_code: int, duration_seconds: float, was_sampled: bool
    ) -> None:
        """Report a completed request's outcome. Called for *every*
        request, sampled or not -- this is the cheap unconditional
        signal described in the module docstring, not itself a sample.
        """
        is_bad = status_code >= 500 or (
            duration_seconds >= self._config.slow_request_threshold_seconds
        )
        if not is_bad:
            return

        with self._lock:
            self._total_bad_outcomes += 1
            self._escalate_locked()

        if not was_sampled:
            # This exact request wasn't sampled, so there's no detailed
            # trace for it -- make sure it's still visible somewhere
            # rather than vanishing entirely.
            logger.warning(
                "Unsampled request had a bad outcome (status=%s, "
                "duration=%.3fs) -- escalating sample rate; this "
                "specific request has no detailed trace, only this log line",
                status_code,
                duration_seconds,
            )

    def _escalate_locked(self) -> None:
        self._total_escalations += 1
        self._escalated_until = time.monotonic() + self._config.escalation_seconds
        self._rate_percent = self._config.max_rate_percent

    def _maybe_adjust_locked(self) -> None:
        elapsed = time.monotonic() - self._window_start
        if elapsed < self._config.adjustment_interval_seconds:
            return

        actual_rate = self._window_sampled / elapsed if elapsed > 0 else 0.0
        target = self._config.target_samples_per_second

        if actual_rate < target:
            # Sampling below target -- room to sample more. Step size
            # is proportional to how far off target we are, so a
            # near-idle service ramps up quickly rather than crawling
            # toward 100% one point at a time.
            self._rate_percent = min(
                self._config.max_rate_percent, self._rate_percent * 1.5 + 1.0
            )
        elif actual_rate > target * 1.2:
            # Meaningfully over target -- ease back down. Multiplicative
            # decrease (not a hard reset) avoids oscillating between two
            # extremes on bursty traffic.
            self._rate_percent = max(
                self._config.min_rate_percent, self._rate_percent * 0.7
            )

        self._window_start = time.monotonic()
        self._window_sampled = 0
        self._window_requests = 0

    def metrics(self) -> dict:
        with self._lock:
            return {
                "current_rate_percent": round(self._rate_percent, 2),
                "target_samples_per_second": self._config.target_samples_per_second,
                "escalated": time.monotonic() < self._escalated_until,
                "total_requests": self._total_requests,
                "total_sampled": self._total_sampled,
                "total_escalations": self._total_escalations,
                "total_bad_outcomes": self._total_bad_outcomes,
            }
