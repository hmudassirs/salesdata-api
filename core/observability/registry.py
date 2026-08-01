"""Health-check and metrics registries.

Moved from core/services/observability.py. Distinct from
core.app.health.HealthCheck (a single hardcoded DB check used by
/api/health) — this is a pluggable registry other subsystems can
register named checks/collectors into, feeding a combined health/metrics
endpoint. No call site registering anything into these was found in
this codebase as reviewed; confirm before assuming this is live.
"""

import json
from datetime import datetime
from typing import Any, Callable, Dict, Optional

from core.observability.prometheus_metrics import HealthStatus


class HealthCheckRegistry:
    """Registry for health check functions."""

    def __init__(self):
        self._checks: Dict[str, Callable] = {}
        self._last_check: Optional[Dict[str, Any]] = None

    def register(self, name: str, check_fn: Callable[[], bool]):
        """Register a health check function."""
        self._checks[name] = check_fn

    async def run_checks(self) -> Dict[str, Any]:
        """Run all registered health checks."""
        results = {
            "status": HealthStatus.HEALTHY,
            "timestamp": datetime.now().isoformat(),
            "checks": {},
        }

        all_healthy = True

        for name, check_fn in self._checks.items():
            try:
                result = check_fn()
                # Handle async functions
                if hasattr(result, "__await__"):
                    result = await result

                results["checks"][name] = {
                    "status": "ok" if result else "failed",
                    "healthy": result,
                }
                all_healthy = all_healthy and result
            except Exception as e:
                results["checks"][name] = {
                    "status": "error",
                    "error": str(e),
                    "healthy": False,
                }
                all_healthy = False

        results["status"] = (
            HealthStatus.HEALTHY if all_healthy else HealthStatus.DEGRADED
        )
        self._last_check = results

        return results

    def get_last_result(self) -> Optional[Dict[str, Any]]:
        """Get last health check result."""
        return self._last_check

    def to_json(self, result: Dict[str, Any]) -> str:
        """Convert health check result to JSON."""
        return json.dumps(result, indent=2)


class MetricsRegistry:
    """Registry for metrics collectors."""

    def __init__(self):
        self._collectors: Dict[str, Callable] = {}
        self._metrics: Optional[Dict[str, Any]] = None

    def register(self, name: str, collector_fn: Callable[[], Dict]):
        """Register a metrics collector."""
        self._collectors[name] = collector_fn

    async def collect(self) -> Dict[str, Any]:
        """Collect all metrics."""
        metrics = {
            "timestamp": datetime.now().isoformat(),
            "metrics": {},
        }

        for name, collector_fn in self._collectors.items():
            try:
                result = collector_fn()
                # Handle async functions
                if hasattr(result, "__await__"):
                    result = await result

                metrics["metrics"][name] = result
            except Exception as e:
                metrics["metrics"][name] = {"error": str(e)}

        self._metrics = metrics
        return metrics

    def get_prometheus_format(self) -> str:
        """Convert metrics to Prometheus text format."""
        if not self._metrics:
            return "# No metrics collected yet\n"

        lines = []
        lines.append("# HELP preparedata_metrics Application metrics")
        lines.append("# TYPE preparedata_metrics gauge")

        for category, data in self._metrics.get("metrics", {}).items():
            if isinstance(data, dict) and "error" not in data:
                for key, value in data.items():
                    if isinstance(value, (int, float)):
                        lines.append(f'preparedata_{category}{{label="{key}"}} {value}')

        return "\n".join(lines) + "\n"

    def to_json(self, metrics: Dict[str, Any]) -> str:
        """Convert metrics to JSON."""
        return json.dumps(metrics, indent=2)


class ObservabilityManager:
    """Unified observability manager for health checks and metrics."""

    def __init__(self):
        self.health = HealthCheckRegistry()
        self.metrics = MetricsRegistry()

    async def get_health(self) -> Dict[str, Any]:
        """Get current health status."""
        return await self.health.run_checks()

    async def get_metrics(self, format: str = "json") -> str:
        """Get metrics in specified format.

        Args:
            format: 'json' or 'prometheus'

        Returns:
            Formatted metrics string
        """
        metrics_data = await self.metrics.collect()

        if format == "prometheus":
            return self.metrics.get_prometheus_format()
        else:
            return self.metrics.to_json(metrics_data)

    def get_health_status_code(self, health_result: Dict[str, Any]) -> int:
        """Get HTTP status code for health result."""
        if health_result["status"] == HealthStatus.HEALTHY:
            return 200
        elif health_result["status"] == HealthStatus.DEGRADED:
            return 503
        else:
            return 503
