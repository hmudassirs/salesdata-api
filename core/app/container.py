# core/app/container.py
"""Dependency injection container for application services.

Renamed from the misspelled `dependancies.py`. This module defines the
`DependencyContainer` class used by `lifespan.py` to register and look
up long-lived services (db session, cache, logging, tracing, audit,
etc.). It is distinct from `dependencies.py`, which holds FastAPI
`Depends(...)` callables for use directly in route signatures.

If anything else in the codebase still imports `core.app.dependancies`,
update it to `core.app.container` — the old module name is removed.
"""

import logging
from typing import Any, Optional

from core.caching.query_result_cache import QueryResultCache
from core.db.session import DatabaseSession
from core.observability.request_logger import RequestLogger
from core.storage.service_db import ServiceDatabase

logger = logging.getLogger(__name__)


class DependencyContainer:
    """Dependency injection container for application services."""

    def __init__(self):
        """Initialize dependency container."""
        self._services: dict[str, Any] = {}
        self._db_session: Optional[DatabaseSession] = None

    def register(self, name: str, service: Any) -> None:
        """Register a service.

        Args:
            name: Service name
            service: Service instance
        """
        self._services[name] = service
        logger.info(f"Registered service: {name}")

    def get(self, name: str) -> Optional[Any]:
        """Get registered service.

        Args:
            name: Service name

        Returns:
            Service instance or None
        """
        return self._services.get(name)

    def set_database_session(self, session: DatabaseSession) -> None:
        """Set database session.

        Args:
            session: DatabaseSession instance
        """
        self._db_session = session
        self.register("db_session", session)

    def get_database_session(self) -> Optional[DatabaseSession]:
        """Get database session.

        Returns:
            DatabaseSession instance or None
        """
        return self._db_session

    def clear(self) -> None:
        """Clear all registered services."""
        self._services.clear()
        self._db_session = None
        logger.info("Cleared all registered services")

    def get_service_db(self) -> Optional[ServiceDatabase]:
        """Get SQLite service database."""
        return self.get("service_db")

    def get_cache_service(self) -> Optional[QueryResultCache]:
        """Get cache service."""
        return self.get("cache_service")

    def get_logging_service(self) -> Optional[RequestLogger]:
        """Get logging service."""
        return self.get("logging_service")
