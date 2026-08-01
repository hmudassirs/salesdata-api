"""Render current registry state as CSV rows.

One row per metric. Counters/gauges get a `value` column; histograms
spread their `snapshot()` fields (count/sum/mean/min/max/p50/...) across
extra columns instead, so every row stays flat (no nested JSON in a
cell). Conforms to `registry.Exporter`, same `path`/`last_output`
contract as `JSONExporter`.
"""

from __future__ import annotations

import csv
import io
from pathlib import Path

from core.performance.exporters.snapshot import MetricSnapshot, build_snapshot
from core.performance.registry import PerformanceRegistry

_HISTOGRAM_FIELDS = ("count", "sum", "mean", "min", "max", "p50", "p90", "p95", "p99")
_FIELDNAMES = ("kind", "name", "tags", "value", *_HISTOGRAM_FIELDS)


class CSVExporter:
    """Export the current counters/gauges/histograms as flat CSV rows."""

    name = "csv"

    def __init__(self, path: str | Path | None = None) -> None:
        self.path = Path(path) if path is not None else None
        self.last_output: str | None = None

    def render(self, registry: PerformanceRegistry) -> str:
        """Return the CSV text for `registry`'s current state."""
        buffer = io.StringIO()
        writer = csv.DictWriter(buffer, fieldnames=_FIELDNAMES, lineterminator="\n")
        writer.writeheader()
        for snapshot in build_snapshot(registry):
            writer.writerow(_snapshot_row(snapshot))
        return buffer.getvalue()

    def export(self, registry: PerformanceRegistry) -> None:
        """Render and, if `path` is set, write the CSV text to disk."""
        self.last_output = self.render(registry)
        if self.path is not None:
            self.path.write_text(self.last_output)


def _snapshot_row(snapshot: MetricSnapshot) -> dict[str, object]:
    row: dict[str, object] = {
        "kind": snapshot.kind,
        "name": snapshot.name,
        "tags": _format_tags(snapshot.tags),
        "value": "" if snapshot.value is None else snapshot.value,
    }
    histogram = snapshot.histogram or {}
    for field in _HISTOGRAM_FIELDS:
        value = histogram.get(field)
        row[field] = "" if value is None else value
    return row


def _format_tags(tags: dict[str, str]) -> str:
    return ";".join(f"{key}={value}" for key, value in sorted(tags.items()))
