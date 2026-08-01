"""Composition root wiring the domain repositories/services together
around one shared ServiceDatabase connection.

This file intentionally contains no business logic of its own — it is
the one place that knows every domain service exists and hands them
out as attributes (service_manager.api_keys, .users, .logging, ...).
Each individual service now lives with its own domain (auth/,
observability/, caching/) instead of all six being defined in one
`service_manager.py`, which is what made this hard to navigate before.
"""

import time
from typing import Any, Dict

from core.auth.api_key_repository import APIKeyRepository
from core.auth.user_repository import UserRepository
from core.caching.query_result_cache import QueryResultCache
from core.db.logger import get_logger
from core.observability.audit import AuditTrail
from core.observability.request_logger import RequestLogger
from core.observability.request_tracer import RequestTracer
from core.storage.service_db import ServiceDatabase

logger = get_logger(__name__)


class ServiceManager:
    """Groups domain services (users, api keys, logging, tracing, caching,
    audit) around a single service database connection.

    ServiceManager does not connect, create tables, or disconnect. It
    expects an already-connected ServiceDatabase and simply wires up the
    domain services on top of it. Connection lifecycle is owned by
    ApplicationLifespan (see lifespan.py's ServiceDatabaseStep).
    """

    def __init__(self, service_db: ServiceDatabase):
        """Initialize service manager.

        Args:
            service_db: An already-connected ServiceDatabase instance.
        """
        self.service_db = service_db
        self.api_keys = APIKeyRepository(self.service_db)
        self.users = UserRepository(self.service_db)
        self.logging = RequestLogger(self.service_db)
        self.tracing = RequestTracer(self.service_db)
        self.caching = QueryResultCache(self.service_db)
        self.audit = AuditTrail(self.service_db)
        # Set by ApplicationLifespan's ServiceDatabaseStep after construction,
        # once the background flush thread is actually running. None here
        # means "no queue yet" — emit_request_observability() falls back to
        # writing synchronously in that case (see core/observability/context.py).
        self.observability_queue = None

    def cleanup(self) -> None:
        """Clean up expired entries and optimize database."""
        try:
            # Clean up expired cache
            expired_cache = self.service_db.cleanup_expired_cache()

            # Clean up old logs (keep last 30 days)
            thirty_days_ago = int(time.time()) - (30 * 24 * 60 * 60)
            old_logs_result = self.service_db.execute(
                "DELETE FROM logs WHERE timestamp < ?", (thirty_days_ago,)
            )
            old_logs_count = old_logs_result.rowcount

            # Clean up old traces (keep last 7 days)
            seven_days_ago = int(time.time()) - (7 * 24 * 60 * 60)
            old_traces_result = self.service_db.execute(
                "DELETE FROM traces WHERE start_time < ?",
                (seven_days_ago * 1000000,),  # Convert to microseconds
            )
            old_traces_count = old_traces_result.rowcount

            # Vacuum database to reclaim space
            self.service_db.execute("VACUUM")

            logger.info(
                f"Service cleanup: {expired_cache} expired cache, {old_logs_count} old logs, {old_traces_count} old traces"
            )

        except Exception as e:
            logger.error(f"Service cleanup failed: {e}")

    def get_stats(self) -> Dict[str, Any]:
        """Get comprehensive service statistics.

        Returns:
            Service statistics
        """
        stats = self.service_db.get_stats()
        stats.update(self.caching.get_cache_stats())
        return stats
