"""Optional exporters that convert registry state to an external form (Phase 10).

Each exporter conforms to `registry.Exporter` (a `name` attribute and an
`export(registry) -> None` method) and reads through the shared
`exporters.snapshot.build_snapshot` helper where applicable. None of
them are registered automatically; call
`registry.register_exporter(SomeExporter(...))` to opt in.
"""

from __future__ import annotations

from core.performance.exporters.console_exporter import ConsoleExporter
from core.performance.exporters.csv_exporter import CSVExporter
from core.performance.exporters.json_exporter import JSONExporter
from core.performance.exporters.otel_exporter import OTelExporter
from core.performance.exporters.prometheus_exporter import PrometheusExporter
from core.performance.exporters.snapshot import MetricSnapshot, build_snapshot

__all__ = [
    "CSVExporter",
    "ConsoleExporter",
    "JSONExporter",
    "MetricSnapshot",
    "OTelExporter",
    "PrometheusExporter",
    "build_snapshot",
]
