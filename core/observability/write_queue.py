"""Background flush queue for request observability writes.

Batching 4 writes into 1 transaction per request (see
core/observability/context.py) cut the *overhead per write*, but it
didn't touch the actual bottleneck: SQLite allows exactly one writer at
a time, regardless of pool size. With N concurrent requests each still
needing their own write transaction, all N queue for that same single
write slot — that queueing time shows up directly as request latency.

This queue removes the write from the request path entirely: producers
(emit_request_observability) just enqueue a record — fast, in-memory,
no I/O — and a single background thread drains the queue periodically,
writing many requests' worth of records in ONE transaction. Response
latency stops being coupled to how many *other* requests also need to
log something.

Trade-off, stated plainly: if the process dies with records still
queued, those log/trace/audit entries are lost. For request
logging/tracing/audit — not the actual data warehouse — that's normally
an acceptable trade for the latency win. Reconsider if you need these
records durable synchronously with the response (e.g. audit entries
required for compliance before an action is considered "complete" —
in that case, write those specific ones synchronously and queue only
the rest).
"""

import queue
import threading
import time
from typing import Any, Callable, Dict, List, Optional

from core.db.logger import get_logger

logger = get_logger(__name__)


class ObservabilityWriteQueue:
    """Queues observability records and flushes them in batches from a
    dedicated background thread."""

    def __init__(
        self,
        service_db,
        write_record: Callable[[Any, Dict[str, Any]], None],
        flush_interval: float = 0.2,
        max_batch: int = 200,
    ):
        """
        Args:
            service_db: ServiceDatabase instance (for .transaction()).
            write_record: Callable(adapter, record) -> None, run once
                per queued record inside the shared transaction. See
                core/observability/context.py's _write_observability_record.
            flush_interval: Seconds between flush attempts.
            max_batch: Cap per flush, so one huge burst doesn't hold the
                write lock for an unbounded amount of time in one go.
        """
        self._service_db = service_db
        self._write_record = write_record
        self._flush_interval = flush_interval
        self._max_batch = max_batch
        self._queue: "queue.Queue[Dict[str, Any]]" = queue.Queue()
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(
            target=self._run, daemon=True, name="observability-flush"
        )
        self._thread.start()
        logger.info(
            "Observability write queue started (flush_interval=%.2fs, max_batch=%d)",
            self._flush_interval,
            self._max_batch,
        )

    def stop(self) -> None:
        """Stop the background thread and flush whatever's left queued."""
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=5.0)
            self._thread = None
        remaining = self._drain(unlimited=True)
        if remaining:
            logger.info(
                "Flushing %d remaining observability record(s) on shutdown",
                len(remaining),
            )
            self._flush_batch(remaining)

    def enqueue(self, record: Dict[str, Any]) -> None:
        """Non-blocking. Called from request-handling code — must never
        do I/O or block on the write lock itself."""
        self._queue.put_nowait(record)

    def _drain(self, unlimited: bool = False) -> List[Dict[str, Any]]:
        batch: List[Dict[str, Any]] = []
        limit = None if unlimited else self._max_batch
        try:
            while limit is None or len(batch) < limit:
                batch.append(self._queue.get_nowait())
        except queue.Empty:
            pass
        return batch

    def _run(self) -> None:
        while not self._stop_event.is_set():
            time.sleep(self._flush_interval)
            batch = self._drain()
            if batch:
                self._flush_batch(batch)

    def _flush_batch(self, batch: List[Dict[str, Any]]) -> None:
        if not batch:
            return
        try:
            with self._service_db.transaction() as adapter:
                for record in batch:
                    try:
                        self._write_record(adapter, record)
                    except Exception:
                        logger.error(
                            "Failed to write one observability record in batch "
                            "(continuing with the rest)",
                            exc_info=True,
                        )
        except Exception:
            logger.error(
                "Failed to flush observability batch of %d record(s)",
                len(batch),
                exc_info=True,
            )
