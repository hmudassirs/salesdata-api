"""Render current registry state in Prometheus text exposition format.

Deliberately hand-formats the exposition-format text itself rather than
depending on `prometheus_client` — per `docs/PerformancePlan.md`, "any
Prometheus or OpenTelemetry bridge is an optional exporter, not a core
dependency", and this also keeps it from sharing (and so risking a name
collision with) `core.observability.prometheus_metrics`'s own
`CollectorRegistry`, which is a separate, existing metrics surface.

Histograms are rendered as their own `_bucket`/`_sum`/`_count` lines in
the standard cumulative-bucket shape (`Histogram.cumulative_counts()`),
not as the streaming-quantile summary the other exporters use, since
that is what Prometheus's own histogram type expects on scrape.
"""

from __future__ import annotations

from pathlib import Path

from core.performance.histogram import StreamingHistogram
from core.performance.registry import PerformanceRegistry

_TYPE_LINE = "# TYPE {name} {prom_type}"


class PrometheusExporter:
    """Export the current counters/gauges/histograms as exposition-format text."""

    name = "prometheus"

    def __init__(self, path: str | Path | None = None) -> None:
        self.path = Path(path) if path is not None else None
        self.last_output: str | None = None

    def render(self, registry: PerformanceRegistry) -> str:
        """Return the Prometheus exposition-format text for `registry`."""
        lines: list[str] = []
        emitted_types: set[str] = set()
        for counter in registry.counters():
            _emit_type(lines, emitted_types, counter.name, "counter")
            lines.append(_sample_line(counter.name, counter.tags, counter.value))
        for gauge in registry.gauges():
            _emit_type(lines, emitted_types, gauge.name, "gauge")
            lines.append(_sample_line(gauge.name, gauge.tags, gauge.value))
        for histogram in registry.histograms():
            _emit_type(lines, emitted_types, histogram.name, "histogram")
            lines.extend(_histogram_lines(histogram))
        return "\n".join(lines) + ("\n" if lines else "")

    def export(self, registry: PerformanceRegistry) -> None:
        """Render and, if `path` is set, write the exposition text to disk."""
        self.last_output = self.render(registry)
        if self.path is not None:
            self.path.write_text(self.last_output)


def _emit_type(
    lines: list[str], emitted: set[str], name: str, prom_type: str
) -> None:
    if name in emitted:
        return
    emitted.add(name)
    lines.append(_TYPE_LINE.format(name=name, prom_type=prom_type))


def _sample_line(name: str, tags: dict[str, str], value: float) -> str:
    return f"{name}{_label_suffix(tags)} {value}"


def _label_suffix(tags: dict[str, str]) -> str:
    if not tags:
        return ""
    labels = ",".join(f'{key}="{value}"' for key, value in sorted(tags.items()))
    return f"{{{labels}}}"


def _histogram_lines(histogram: StreamingHistogram) -> list[str]:
    name = histogram.name
    tags = histogram.tags
    lines: list[str] = []
    for bound, cumulative in histogram.histogram.cumulative_counts().items():
        bucket_tags = {**tags, "le": bound}
        lines.append(f"{name}_bucket{_label_suffix(bucket_tags)} {cumulative}")
    lines.append(_sample_line(f"{name}_sum", tags, histogram.histogram.total))
    lines.append(_sample_line(f"{name}_count", tags, histogram.histogram.count))
    return lines
