"""Bridge current registry state onto OpenTelemetry metric instruments.

Uses only `opentelemetry-api` (already a project dependency via
`core.observability.otel`), never a specific SDK exporter (Jaeger,
OTLP, ...): whatever `MeterProvider` the application configured (see
`core.observability.otel.OpenTelemetryManager`) is where these
instruments end up, or nowhere if none was configured — `metrics.get_meter`
returns a working no-op meter in that case, so this exporter is safe to
construct and call unconditionally.

Counters are cumulative totals in this subsystem's aggregator, but
OTel's `Counter.add()` expects a *delta* to add since the last call.
This exporter tracks the last value it exported per counter identity
and adds only the difference (treating a decrease — a process
restart resetting the underlying counter — as a fresh total rather
than a negative add, which the OTel API rejects).

Gauges (and, for want of a native "pre-aggregated histogram" API,
each histogram's `count`/`sum`/`mean`/quantile fields, one per
`stat` label) are exported via OTel's synchronous `Gauge.set()`, which
does not require delta bookkeeping.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from opentelemetry import metrics

from core.performance.registry import PerformanceRegistry

if TYPE_CHECKING:
    from opentelemetry.metrics import Counter as OTelCounter
    from opentelemetry.metrics import Meter

    # opentelemetry.metrics does not publicly export a "Gauge" instrument
    # type (only the private `_Gauge`), so the synchronous gauge created by
    # `Meter.create_gauge()` is typed structurally via `Any` here.
    OTelGauge = Any

_CounterKey = tuple[str, tuple[tuple[str, str], ...]]


def _counter_key(name: str, tags: dict[str, str]) -> _CounterKey:
    return name, tuple(sorted(tags.items()))


class OTelExporter:
    """Export the current counters/gauges/histograms as OTel instruments."""

    name = "otel"

    def __init__(
        self, meter: Meter | None = None, meter_name: str = "core.performance"
    ) -> None:
        self._meter: Meter = (
            meter if meter is not None else metrics.get_meter(meter_name)
        )
        self._counter_instruments: dict[str, OTelCounter] = {}
        self._gauge_instruments: dict[str, OTelGauge] = {}
        self._last_counter_values: dict[_CounterKey, float] = {}

    def export(self, registry: PerformanceRegistry) -> None:
        """Push every current counter/gauge/histogram-stat onto OTel instruments."""
        for counter in registry.counters():
            self._export_counter(counter.name, counter.tags, counter.value)
        for gauge in registry.gauges():
            self._export_gauge(gauge.name, gauge.tags, gauge.value)
        for histogram in registry.histograms():
            for stat, value in histogram.snapshot().items():
                if value is None:
                    continue
                self._export_gauge(
                    histogram.name, {**histogram.tags, "stat": stat}, float(value)
                )

    def _export_counter(self, name: str, tags: dict[str, str], total: float) -> None:
        key = _counter_key(name, tags)
        previous = self._last_counter_values.get(key, 0.0)
        delta = total - previous if total >= previous else total
        self._last_counter_values[key] = total
        if delta <= 0:
            return
        instrument = self._counter_instruments.get(name)
        if instrument is None:
            instrument = self._meter.create_counter(name)
            self._counter_instruments[name] = instrument
        instrument.add(delta, attributes=_attributes(tags))

    def _export_gauge(self, name: str, tags: dict[str, str], value: float) -> None:
        instrument = self._gauge_instruments.get(name)
        if instrument is None:
            instrument = self._meter.create_gauge(name)
            self._gauge_instruments[name] = instrument
        instrument.set(value, attributes=_attributes(tags))


def _attributes(tags: dict[str, str]) -> dict[str, Any]:
    return dict(tags)
