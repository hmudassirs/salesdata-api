"""Configuration for the framework-independent performance subsystem."""

from __future__ import annotations

import os
import random
from dataclasses import dataclass

from .constants import (
    DEFAULT_MAX_REQUEST_HISTORY,
    DEFAULT_MAX_TRACE_NODES,
    DEFAULT_SAMPLE_RATE_PERCENT,
    ENV_ENABLED,
    ENV_MAX_REQUEST_HISTORY,
    ENV_MAX_TRACE_NODES,
    ENV_SAMPLE_RATE_PERCENT,
    PERCENT_MAXIMUM,
    PERCENT_MINIMUM,
)
from .exceptions import (
    _CONFIGURATION_HISTORY_MESSAGE,
    _CONFIGURATION_SAMPLE_RATE_MESSAGE,
    _CONFIGURATION_TRACE_NODES_MESSAGE,
    PerformanceConfigurationError,
)


@dataclass(frozen=True, slots=True)
class PerformanceConfig:
    """Control whether profiling is active and how much data it may retain."""

    enabled: bool = False
    sample_rate_percent: int = DEFAULT_SAMPLE_RATE_PERCENT
    max_trace_nodes: int = DEFAULT_MAX_TRACE_NODES
    max_request_history: int = DEFAULT_MAX_REQUEST_HISTORY
    collect_memory: bool = False
    collect_gc: bool = False
    collect_threads: bool = False
    collect_cpu: bool = False
    collect_asyncio: bool = False
    collect_process: bool = False

    def __post_init__(self) -> None:
        """Validate configuration values at construction time."""
        if not PERCENT_MINIMUM <= self.sample_rate_percent <= PERCENT_MAXIMUM:
            raise PerformanceConfigurationError(_CONFIGURATION_SAMPLE_RATE_MESSAGE)
        if self.max_trace_nodes < 1:
            raise PerformanceConfigurationError(_CONFIGURATION_TRACE_NODES_MESSAGE)
        if self.max_request_history < 0:
            raise PerformanceConfigurationError(_CONFIGURATION_HISTORY_MESSAGE)

    def should_sample(self) -> bool:
        """Decide whether one request should be profiled.

        Disabled configuration always returns False without consulting
        the random source, keeping the disabled path allocation- and
        call-free beyond this single boolean check.
        """
        if not self.enabled:
            return False
        if self.sample_rate_percent >= PERCENT_MAXIMUM:
            return True
        if self.sample_rate_percent <= PERCENT_MINIMUM:
            return False
        return random.randint(1, PERCENT_MAXIMUM) <= self.sample_rate_percent

    @classmethod
    def from_env(cls) -> PerformanceConfig:
        """Build configuration from the documented PERF_* environment variables."""
        return cls(
            enabled=_read_bool(ENV_ENABLED, False),
            sample_rate_percent=_read_int(
                ENV_SAMPLE_RATE_PERCENT,
                DEFAULT_SAMPLE_RATE_PERCENT,
            ),
            max_trace_nodes=_read_int(ENV_MAX_TRACE_NODES, DEFAULT_MAX_TRACE_NODES),
            max_request_history=_read_int(
                ENV_MAX_REQUEST_HISTORY,
                DEFAULT_MAX_REQUEST_HISTORY,
            ),
        )


def _read_bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    normalized = value.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise PerformanceConfigurationError(f"{name} must be a boolean value")  # noqa: TRY003


def _read_int(name: str, default: int) -> int:
    value = os.getenv(name)
    if value is None:
        return default
    try:
        return int(value)
    except ValueError as error:
        raise PerformanceConfigurationError(f"{name} must be an integer") from error  # noqa: TRY003
