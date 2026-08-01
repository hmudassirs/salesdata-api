"""Typed value objects shared by the performance subsystem."""

from __future__ import annotations

from dataclasses import dataclass
from typing import NewType

RequestId = NewType("RequestId", str)
TraceId = NewType("TraceId", str)
MetricName = NewType("MetricName", str)
ConnectionId = NewType("ConnectionId", int)
QueryId = NewType("QueryId", int)
DurationNS = NewType("DurationNS", int)
TimestampNS = NewType("TimestampNS", int)
MetricValue = int | float
Tags = dict[str, str]
Metadata = dict[str, object]


@dataclass(frozen=True, slots=True)
class MetricTag:
    """An immutable metric label represented as a key and value pair."""

    key: str
    value: str
