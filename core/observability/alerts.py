"""Threshold-based operational alerts (roadmap P1-5).

Evaluates the metrics already exposed on `/api/health` (pool, executor,
query-concurrency, cache-persistence, adaptive-sampler) against
configurable thresholds and returns a flat list of currently-firing
`Alert`s. Two kinds of check:

    - Gauge checks (saturation, utilization, queue depth vs. capacity)
      compare the current value directly -- these fire for as long as
      the condition holds and clear as soon as it doesn't.
    - Counter checks (pool timeouts, broken-connection evictions,
      dropped cache writes) compare against the value from the
      *previous* evaluation, so a threshold is only crossed by *new*
      occurrences since last checked -- a counter that's nonzero
      because of one historical blip does not alert forever.

`AlertEvaluator` is the stateful piece that remembers prior counter
values between calls; `evaluate_alerts()` is a pure function of
"current snapshot + previous counters" for testability.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, TypedDict

logger = logging.getLogger(__name__)


class AlertDict(TypedDict):
    code: str
    severity: str  # "warning" | "critical"
    message: str
    value: Any
    threshold: Any


@dataclass(frozen=True)
class AlertThresholds:
    pool_saturation_warning: float = 0.80
    pool_saturation_critical: float = 0.95
    executor_utilization_warning: float = 0.90
    query_concurrency_warning: float = 0.90
    persistence_queue_depth_warning_ratio: float = 0.75  # of maxsize


@dataclass
class Alert:
    code: str
    severity: str
    message: str
    value: Any
    threshold: Any

    def to_dict(self) -> AlertDict:
        return {
            "code": self.code,
            "severity": self.severity,
            "message": self.message,
            "value": self.value,
            "threshold": self.threshold,
        }


@dataclass
class AlertEvaluator:
    thresholds: AlertThresholds = field(default_factory=AlertThresholds)
    _prev_counters: Dict[str, int] = field(default_factory=dict, repr=False)
    _firing: set = field(default_factory=set, repr=False)

    def evaluate(
        self,
        *,
        pool_metrics: Optional[dict],
        executor_metrics: Optional[dict],
        query_concurrency_metrics: Optional[dict],
        cache_persistence_metrics: Optional[dict],
        adaptive_sampler_metrics: Optional[dict],
        persistence_queue_maxsize: int = 1000,
    ) -> List[AlertDict]:
        alerts: List[Alert] = []

        if pool_metrics:
            alerts.extend(self._check_pool(pool_metrics))
        if executor_metrics:
            alerts.extend(self._check_executors(executor_metrics))
        if query_concurrency_metrics:
            alerts.extend(self._check_query_concurrency(query_concurrency_metrics))
        if cache_persistence_metrics:
            alerts.extend(
                self._check_persistence(
                    cache_persistence_metrics, persistence_queue_maxsize
                )
            )
        if adaptive_sampler_metrics and adaptive_sampler_metrics.get("escalated"):
            alerts.append(
                Alert(
                    code="sampling_escalated",
                    severity="warning",
                    message=(
                        "Adaptive performance sampling is escalated -- "
                        "recent requests were slow or errored"
                    ),
                    value=True,
                    threshold=False,
                )
            )

        self._log_newly_firing(alerts)
        return [a.to_dict() for a in alerts]

    def _check_pool(self, m: dict) -> List[Alert]:
        alerts = []
        saturation = m.get("saturation", 0.0)
        if saturation >= self.thresholds.pool_saturation_critical:
            alerts.append(
                Alert(
                    "pool_saturated",
                    "critical",
                    f"DB connection pool is at {saturation:.0%} saturation",
                    saturation,
                    self.thresholds.pool_saturation_critical,
                )
            )
        elif saturation >= self.thresholds.pool_saturation_warning:
            alerts.append(
                Alert(
                    "pool_under_pressure",
                    "warning",
                    f"DB connection pool is at {saturation:.0%} saturation",
                    saturation,
                    self.thresholds.pool_saturation_warning,
                )
            )

        alerts.extend(
            self._check_counter_delta(
                "pool_timeouts",
                m.get("timed_out_acquires", 0),
                severity="critical",
                message="New pool acquire timeouts since last check",
            )
        )
        alerts.extend(
            self._check_counter_delta(
                "pool_broken_connections",
                m.get("broken_connections_evicted", 0),
                severity="warning",
                message="New broken DB connections evicted since last check",
            )
        )
        return alerts

    def _check_executors(self, executors: dict) -> List[Alert]:
        alerts = []
        for name, m in executors.items():
            max_workers = m.get("max_workers", 0) or 1
            active = m.get("active", 0)
            queued = m.get("approx_queue_depth", 0)
            utilization = active / max_workers
            if (
                utilization >= self.thresholds.executor_utilization_warning
                or queued > 0
            ):
                alerts.append(
                    Alert(
                        f"executor_{name}_saturated",
                        "warning",
                        f"'{name}' executor is at {utilization:.0%} utilization "
                        f"with {queued} job(s) queued",
                        {"utilization": round(utilization, 2), "queued": queued},
                        self.thresholds.executor_utilization_warning,
                    )
                )
        return alerts

    def _check_query_concurrency(self, classes: dict) -> List[Alert]:
        alerts = []
        for cost, m in classes.items():
            limit = m.get("limit", 0) or 1
            in_use = m.get("in_use", 0)
            ratio = in_use / limit
            if ratio >= self.thresholds.query_concurrency_warning:
                alerts.append(
                    Alert(
                        f"query_concurrency_{cost}_saturated",
                        "warning",
                        f"'{cost}' query concurrency limit is at {ratio:.0%} usage",
                        ratio,
                        self.thresholds.query_concurrency_warning,
                    )
                )
        return alerts

    def _check_persistence(self, m: dict, maxsize: int) -> List[Alert]:
        alerts = []
        depth = m.get("queue_depth", 0)
        ratio = depth / max(1, maxsize)
        if ratio >= self.thresholds.persistence_queue_depth_warning_ratio:
            alerts.append(
                Alert(
                    "persistence_queue_backlog",
                    "warning",
                    f"Cache persistence queue is at {ratio:.0%} of capacity",
                    depth,
                    int(maxsize * self.thresholds.persistence_queue_depth_warning_ratio),
                )
            )
        alerts.extend(
            self._check_counter_delta(
                "persistence_dropped",
                m.get("dropped", 0),
                severity="warning",
                message="Cache persistence queue dropped new writes since last check",
            )
        )
        alerts.extend(
            self._check_counter_delta(
                "persistence_failed",
                m.get("failed", 0),
                severity="warning",
                message="Cache persistence jobs failed since last check",
            )
        )
        return alerts

    def _check_counter_delta(
        self, key: str, current_value: int, *, severity: str, message: str
    ) -> List[Alert]:
        """Fire only if `current_value` increased since the last call
        with this `key` -- see module docstring on why counters are
        delta-checked instead of threshold-checked directly."""
        previous = self._prev_counters.get(key, current_value)
        delta = current_value - previous
        self._prev_counters[key] = current_value
        if delta > 0:
            return [Alert(key, severity, message, delta, 0)]
        return []

    def _log_newly_firing(self, alerts: List[Alert]) -> None:
        """Log each alert only the first time it starts firing (not on
        every health check while it continues), and log its resolution
        once it stops -- edge-triggered, so this doesn't spam logs."""
        current_codes = {a.code for a in alerts}
        for alert in alerts:
            if alert.code not in self._firing:
                logger.warning(
                    "ALERT [%s] %s (value=%s, threshold=%s)",
                    alert.severity,
                    alert.message,
                    alert.value,
                    alert.threshold,
                )
        for resolved_code in self._firing - current_codes:
            logger.info("ALERT RESOLVED [%s]", resolved_code)
        self._firing = current_codes


_evaluator: Optional[AlertEvaluator] = None


def get_alert_evaluator() -> AlertEvaluator:
    global _evaluator
    if _evaluator is None:
        _evaluator = AlertEvaluator()
    return _evaluator
