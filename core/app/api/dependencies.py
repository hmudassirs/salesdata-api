"""FastAPI `Depends(...)` callables for use directly in route signatures.

Distinct from `core.app.container.DependencyContainer` (the long-lived
service registry populated at startup by `lifespan.py`). This module
adapts values out of that container/app.state into per-request
dependencies that routes can declare with `Depends(...)`.

CurrentUser/GetCurrentUser moved to core.auth.dependencies — that's an
auth concept, not generic app wiring; re-exported here only for
backward compatibility with any code still importing it from this path.
"""

from typing import AsyncGenerator, Optional

from fastapi import Depends, HTTPException, Request

from core.auth.dependencies import CurrentUser, GetCurrentUser, get_current_user
from core.db.session import DatabaseSession

__all__ = [
    "GetDB",
    "GetServiceManager",
    "GetSettings",
    "CurrentUser",
    "GetCurrentUser",
    "get_current_user",
    "get_db_session",
    "get_service_manager",
    "get_settings",
]


async def get_db_session(request: Request) -> AsyncGenerator[DatabaseSession, None]:
    """Provide DatabaseSession to endpoints via dependency injection.

    Args:
        request: FastAPI request object

    Yields:
        DatabaseSession instance

    Raises:
        HTTPException: 503 if the data warehouse step never started
            (e.g. no db_config was provided at startup).
    """
    db_session: Optional[DatabaseSession] = getattr(
        request.app.state, "db_session", None
    )
    if not db_session:
        raise HTTPException(
            status_code=503, detail="Database is not configured or unavailable"
        )
    yield db_session


def get_service_manager(request: Request):
    """Provide the ServiceManager (auth, caching, logging, tracing, audit).

    Args:
        request: FastAPI request object

    Returns:
        ServiceManager instance

    Raises:
        HTTPException: 503 if the service database step never started.
    """
    service_manager = getattr(request.app.state, "service_manager", None)
    if not service_manager:
        raise HTTPException(
            status_code=503, detail="Service manager is not configured or unavailable"
        )
    return service_manager


def get_settings(request: Request):
    """Provide the application's AppSettings.

    Args:
        request: FastAPI request object

    Returns:
        AppSettings instance
    """
    settings = getattr(request.app.state, "settings", None)
    if not settings:
        raise RuntimeError("AppSettings not configured")
    return settings


# Create dependency callables for endpoints
GetDB = Depends(get_db_session)
GetServiceManager = Depends(get_service_manager)
GetSettings = Depends(get_settings)
