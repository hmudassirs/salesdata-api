"""Turn completed metric events into aggregate counter/gauge/histogram state.

`MetricsAggregator` is deliberately the only place that owns mutable
aggregate state. `PerformanceTimer`, `Trace`, and `RequestProfiler` never
touch it directly; they only ever produce immutable `MetricPoint` values.
The registry hands the aggregator batches of points once a request (or a
manual flush) completes, which is why aggregation never needs a
process-wide lock on the request's hot path: by the time the aggregator
runs, the request-local data is already finished and immutable.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .constants import DEFAULT_STREAMING_QUANTILES
from .enums import MetricType
from .histogram import StreamingHistogram
from .metric import Counter, Gauge, MetricPoint, ensure_same_type
from .types import MetricName, Tags

MetricKey = tuple[MetricName, tuple[tuple[str, str], ...]]


def _key(name: MetricName, tags: Tags) -> MetricKey:
    """Build a hashable identity for a metric name plus its tag set."""
    return name, tuple(sorted(tags.items()))


@dataclass(slots=True)
class MetricsAggregator:
    """Own counters, gauges, and histograms and fold points into them."""

    quantiles: tuple[float, ...] = DEFAULT_STREAMING_QUANTILES
    _counters: dict[MetricKey, Counter] = field(default_factory=dict, repr=False)
    _gauges: dict[MetricKey, Gauge] = field(default_factory=dict, repr=False)
    _histograms: dict[MetricKey, StreamingHistogram] = field(
        default_factory=dict, repr=False
    )
    _metric_types: dict[MetricName, MetricType] = field(
        default_factory=dict, repr=False
    )

    def ingest(self, points: list[MetricPoint]) -> None:
        """Fold a batch of completed metric points into aggregate state."""
        for point in points:
            self.ingest_one(point)

    def ingest_one(self, point: MetricPoint) -> None:
        """Fold a single metric point into the matching aggregate."""
        existing = self._metric_types.get(point.name)
        if existing is None:
            self._metric_types[point.name] = point.metric_type
        else:
            ensure_same_type(existing, point.metric_type, point.name)

        if point.metric_type is MetricType.COUNTER:
            self._counter_for(point.name, point.tags).increment(point.value)
        elif point.metric_type is MetricType.GAUGE:
            self._gauge_for(point.name, point.tags).set(point.value)
        elif point.metric_type in (MetricType.HISTOGRAM, MetricType.TIMER):
            self._histogram_for(point.name, point.tags).observe(point.value)
        # EVENT and TRACE points are not aggregated numerically; the
        # registry/collector layer retains those as raw records instead.

    def _counter_for(self, name: MetricName, tags: Tags) -> Counter:
        key = _key(name, tags)
        counter = self._counters.get(key)
        if counter is None:
            counter = Counter(name=name, tags=dict(tags))
            self._counters[key] = counter
        return counter

    def _gauge_for(self, name: MetricName, tags: Tags) -> Gauge:
        key = _key(name, tags)
        gauge = self._gauges.get(key)
        if gauge is None:
            gauge = Gauge(name=name, tags=dict(tags))
            self._gauges[key] = gauge
        return gauge

    def _histogram_for(self, name: MetricName, tags: Tags) -> StreamingHistogram:
        key = _key(name, tags)
        histogram = self._histograms.get(key)
        if histogram is None:
            histogram = StreamingHistogram(
                name=name, tags=dict(tags), quantiles=self.quantiles
            )
            self._histograms[key] = histogram
        return histogram

    def counters(self) -> list[Counter]:
        """Return a snapshot list of every counter seen so far."""
        return list(self._counters.values())

    def gauges(self) -> list[Gauge]:
        """Return a snapshot list of every gauge seen so far."""
        return list(self._gauges.values())

    def histograms(self) -> list[StreamingHistogram]:
        """Return a snapshot list of every histogram seen so far."""
        return list(self._histograms.values())

    def reset(self) -> None:
        """Discard all aggregate state, e.g. between benchmark runs."""
        self._counters.clear()
        self._gauges.clear()
        self._histograms.clear()
        self._metric_types.clear()
