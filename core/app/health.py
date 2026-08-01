# core/app/health.py
"""Application health check utilities."""

import logging
from typing import Optional

from core.db.session import DatabaseSession

logger = logging.getLogger(__name__)


class HealthCheck:
    """Application health check service."""

    def __init__(self, db_session: Optional[DatabaseSession] = None):
        """Initialize health check service.

        Args:
            db_session: Optional database session for health checks
        """
        self.db_session = db_session
        self.status = "unknown"

    async def check_async(self) -> dict[str, bool | str]:
        """Perform async health checks.

        Returns:
            Health status dictionary
        """
        health_status = {
            "status": "healthy",
            "database": False,
        }

        try:
            if self.db_session:
                is_healthy = await self.db_session.health_check()
                health_status["database"] = is_healthy
                if not is_healthy:
                    health_status["status"] = "degraded"
        except Exception as e:
            logger.error(f"Health check failed: {e}")
            health_status["status"] = "unhealthy"
            health_status["error"] = str(e)

        return health_status

    def check_sync(self) -> dict[str, bool | str]:
        """Perform sync health checks.

        Returns:
            Health status dictionary
        """
        health_status = {
            "status": "healthy",
            "database": False,
        }

        try:
            if self.db_session:
                is_healthy = self.db_session.health_check_sync()
                health_status["database"] = is_healthy
                if not is_healthy:
                    health_status["status"] = "degraded"
        except Exception as e:
            logger.error(f"Health check failed: {e}")
            health_status["status"] = "unhealthy"
            health_status["error"] = str(e)

        return health_status
