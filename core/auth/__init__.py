"""Authentication and authorization: users, API keys, JWT, middleware.

Everything about "who is this caller" now lives here, instead of being
spread across core/services/service_db.py, service_manager.py,
core/auth/service.py, and core/app/api/app.py.
"""

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from core.auth.api_key_service import APIKeyService
    from core.auth.dependencies import CurrentUser, GetCurrentUser, get_current_user

__all__ = [
    "APIKeyService",
    "CurrentUser",
    "GetCurrentUser",
    "get_current_user",
]


def __getattr__(name: str) -> object:
    """Lazily resolve the four names above on first access (PEP 562).

    This package used to import `APIKeyService`/`CurrentUser`/etc.
    eagerly at module load time. That made merely running
    `import core.auth` (or importing any single submodule of it, e.g.
    `core.auth.passwords`) eagerly pull in the whole composition chain:
    `core.auth.api_key_service` -> `core.service_registry` ->
    `core.auth.api_key_repository` -> `core.storage.service_db`. If
    `core.storage.service_db` was the module *already* mid-import when
    that chain reached it (its own `from core.auth.passwords import
    hash_password` is exactly the kind of import that used to trigger
    this), Python would find a partially-initialized module missing the
    `ServiceDatabase` class it hadn't defined yet, raising a circular-
    import `ImportError` — reproducible via a bare `import core.storage`
    as the first thing touching either subsystem in a fresh process.
    Deferring these four names to first attribute access means
    `import core.auth` (and anything importing one specific submodule of
    it) no longer forces that chain to run at all; it only runs if/when
    something actually asks for `core.auth.APIKeyService` and friends.
    """
    if name == "APIKeyService":
        from core.auth.api_key_service import APIKeyService  # noqa: PLC0415

        return APIKeyService
    if name in ("CurrentUser", "GetCurrentUser", "get_current_user"):
        from core.auth.dependencies import (  # noqa: PLC0415
            CurrentUser,
            GetCurrentUser,
            get_current_user,
        )

        return {
            "CurrentUser": CurrentUser,
            "GetCurrentUser": GetCurrentUser,
            "get_current_user": get_current_user,
        }[name]
    msg = f"module {__name__!r} has no attribute {name!r}"
    raise AttributeError(msg)
