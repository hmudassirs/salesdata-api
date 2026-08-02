import enum
import time
from dataclasses import dataclass, field
from typing import Any


class ConnectionState(enum.Enum):
    """Explicit connection lifecycle states (roadmap 6.2 / P1-1).

    Previously this pool had no state model at all -- a connection was
    either "in `_available`" or "in `_in_use`", with no way to represent
    "this connection just failed and must never be handed out again."
    That's what let a broken connection silently cycle back into
    `_available` on release and get handed to the next caller, who'd
    then fail for a reason that had nothing to do with their own query.
    """

    IDLE = "idle"
    IN_USE = "in_use"
    CREATING = "creating"
    CLOSING = "closing"
    CLOSED = "closed"
    BROKEN = "broken"


@dataclass(slots=True)
class PoolConnection:
    created_at: float
    connection: Any
    checked_out_at: float
    state: ConnectionState = ConnectionState.IN_USE
    # Consecutive connection-level failures observed while this specific
    # connection was checked out. Reset to 0 on a clean release; a
    # connection is evicted (not returned to `_available`) as soon as
    # the caller reports one via `pool.release(conn, broken=True)`, so
    # in practice this rarely exceeds 1 -- it exists mainly as a
    # diagnostic signal in metrics/logs for "how often is this
    # happening," not as a multi-strike tolerance policy.
    consecutive_errors: int = field(default=0)


class MaxConnectionsExceeded(RuntimeError):
    pass


def now() -> float:
    return time.monotonic()


# Substrings that, when found in a connection-level exception's type
# name or message, indicate the underlying transport/connection itself
# is dead -- as opposed to the query being malformed, a constraint
# violation, or any other error that says nothing about the connection.
# This is a deliberately conservative allowlist rather than "anything
# that isn't a known query-error type": misclassifying a broken
# connection as fine just means the next caller on it hits the same
# error one more time (annoying, not dangerous); misclassifying a fine
# connection as broken discards a perfectly good connection (wasteful,
# also not dangerous). Both failure directions are cheap, so erring
# toward "only flag what we're confident about" avoids evicting healthy
# connections over an ordinary SQL syntax/constraint error.
_CONNECTION_LEVEL_SIGNALS = (
    "connection is closed",
    "connection already closed",
    "connection closed",
    "connection reset",
    "broken pipe",
    "not connected",
    "no connection to the server",
    "server closed the connection",
    "could not connect",
    "connection refused",
    "eof occurred",
    "ssl error",
    "socket",
)


def is_connection_level_error(exc: BaseException) -> bool:
    """Best-effort classification: does `exc` indicate the pooled
    connection itself is dead (should be evicted), as opposed to an
    ordinary query-level failure (bad SQL, constraint violation, type
    error) that says nothing about the connection's health?

    Heuristic, not exhaustive -- see module-level comment on
    `_CONNECTION_LEVEL_SIGNALS` for why that's an intentional,
    asymmetric-risk tradeoff rather than a gap to close later.
    """
    text = f"{type(exc).__name__}: {exc}".lower()
    return any(signal in text for signal in _CONNECTION_LEVEL_SIGNALS)
