"""Render current registry state as a human-readable text report.

Meant for local development and ad-hoc debugging: a plain-text summary
written to a stream (`sys.stdout` by default). Conforms to
`registry.Exporter`; the rendered text is also kept on `self.last_output`
so tests don't need to capture the stream.
"""

from __future__ import annotations

import sys
from typing import TextIO

from core.performance.exporters.snapshot import MetricSnapshot, build_snapshot
from core.performance.registry import PerformanceRegistry


class ConsoleExporter:
    """Print the current counters/gauges/histograms as a text report."""

    name = "console"

    def __init__(self, stream: TextIO | None = None) -> None:
        self.stream = stream if stream is not None else sys.stdout
        self.last_output: str | None = None

    def render(self, registry: PerformanceRegistry) -> str:
        """Return the formatted text report for `registry`'s current state."""
        snapshots = build_snapshot(registry)
        lines = ["Performance report", "=" * 18]
        lines.extend(self._section("Counters", snapshots, "counter"))
        lines.extend(self._section("Gauges", snapshots, "gauge"))
        lines.extend(self._section("Histograms", snapshots, "histogram"))
        return "\n".join(lines) + "\n"

    def export(self, registry: PerformanceRegistry) -> None:
        """Render and write the text report to the configured stream."""
        self.last_output = self.render(registry)
        self.stream.write(self.last_output)

    @staticmethod
    def _section(
        title: str, snapshots: list[MetricSnapshot], kind: str
    ) -> list[str]:
        matching = [s for s in snapshots if s.kind == kind]
        lines = [f"\n{title}:"]
        if not matching:
            lines.append("  (none)")
            return lines
        for snapshot in matching:
            tag_suffix = f" {dict(snapshot.tags)}" if snapshot.tags else ""
            if kind == "histogram":
                lines.append(f"  {snapshot.name}{tag_suffix}: {snapshot.histogram}")
            else:
                lines.append(f"  {snapshot.name}{tag_suffix}: {snapshot.value}")
        return lines
