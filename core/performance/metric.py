"""Counter, gauge, and point-in-time metric primitives.

These types are intentionally dumb: they hold and mutate a value and
nothing else. They never sample, aggregate percentiles, export, or read
the clock. `MetricsAggregator` (see `aggregator.py`) is the only thing
that turns a stream of `MetricPoint` events into updated `Counter`,
`Gauge`, and `Histogram` state, and it does so outside the request path.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .enums import MetricType, MetricUnit
from .exceptions import _METRIC_TYPE_MISMATCH_MESSAGE, MetricStateError
from .types import Metadata, MetricName, MetricValue, Tags, TimestampNS


@dataclass(slots=True)
class Counter:
    """A monotonically increasing named value."""

    name: MetricName
    tags: Tags = field(default_factory=dict)
    value: float = 0.0

    def increment(self, amount: MetricValue = 1) -> float:
        """Add a non-negative amount to the counter and return the total."""
        if amount < 0:
            raise MetricStateError("counter increments cannot be negative")  # noqa: TRY003
        self.value += amount
        return self.value


@dataclass(slots=True)
class Gauge:
    """A named value that can move up or down."""

    name: MetricName
    tags: Tags = field(default_factory=dict)
    value: float = 0.0

    def set(self, value: MetricValue) -> float:
        """Replace the gauge value and return it."""
        self.value = float(value)
        return self.value

    def increment(self, amount: MetricValue = 1) -> float:
        """Add `amount` (may be negative) to the gauge and return the total."""
        self.value += amount
        return self.value

    def decrement(self, amount: MetricValue = 1) -> float:
        """Subtract `amount` from the gauge and return the total."""
        self.value -= amount
        return self.value


@dataclass(frozen=True, slots=True)
class MetricPoint:
    """One immutable emitted measurement, produced on the request path.

    `MetricPoint` is the only metric-related object created while a
    request is in flight. It is a plain value: creating one never
    touches a registry, aggregator, or exporter. Handoff happens later,
    in bulk, when the owning request finishes.
    """

    name: MetricName
    metric_type: MetricType
    value: MetricValue
    timestamp_ns: TimestampNS
    unit: MetricUnit = MetricUnit.COUNT
    tags: Tags = field(default_factory=dict)
    metadata: Metadata = field(default_factory=dict)


def ensure_same_type(
    existing_type: MetricType, incoming_type: MetricType, name: MetricName
) -> None:
    """Raise `MetricStateError` if a metric name is reused with a new type."""
    if existing_type is not incoming_type:
        raise MetricStateError(f"{name}: {_METRIC_TYPE_MISMATCH_MESSAGE}")  # noqa: TRY003
