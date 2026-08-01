"""Render current registry state as a JSON document.

Conforms to `registry.Exporter`: `export(registry)` renders and, if
`path` was given at construction, writes the result to that file.
Either way, the rendered text is kept on `self.last_output` so callers
(tests, a debug endpoint, a benchmark report) can read it back without
touching the filesystem.
"""

from __future__ import annotations

import json
from pathlib import Path

from core.performance.exporters.snapshot import MetricSnapshot, build_snapshot
from core.performance.registry import PerformanceRegistry


class JSONExporter:
    """Export the current counters/gauges/histograms as one JSON object."""

    name = "json"

    def __init__(
        self, path: str | Path | None = None, *, indent: int | None = 2
    ) -> None:
        self.path = Path(path) if path is not None else None
        self.indent = indent
        self.last_output: str | None = None

    def render(self, registry: PerformanceRegistry) -> str:
        """Return the JSON text for `registry`'s current state."""
        snapshots = build_snapshot(registry)
        document = {
            "counters": [_snapshot_dict(s) for s in snapshots if s.kind == "counter"],
            "gauges": [_snapshot_dict(s) for s in snapshots if s.kind == "gauge"],
            "histograms": [
                _snapshot_dict(s) for s in snapshots if s.kind == "histogram"
            ],
        }
        return json.dumps(document, indent=self.indent, sort_keys=True)

    def export(self, registry: PerformanceRegistry) -> None:
        """Render and, if `path` is set, write the JSON document to disk."""
        self.last_output = self.render(registry)
        if self.path is not None:
            self.path.write_text(self.last_output)


def _snapshot_dict(snapshot: MetricSnapshot) -> dict[str, object]:
    result: dict[str, object] = {"name": snapshot.name, "tags": snapshot.tags}
    if snapshot.kind == "histogram":
        result["histogram"] = snapshot.histogram
    else:
        result["value"] = snapshot.value
    return result
