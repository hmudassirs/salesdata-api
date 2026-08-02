"""API-key / JWT authentication middleware.

Extracted from core/app/api/app.py's single `log_requests` middleware,
which mixed auth enforcement with observability logging in one
function. This is the half that decides who's allowed to make the
request; see core/observability/middleware.py for the half that
records what happened after.
"""

import asyncio

from core.concurrency.executors import run_in_service_executor
import time

import jwt
from fastapi import FastAPI, Request, Response
from starlette.status import HTTP_401_UNAUTHORIZED

from core.auth.api_key_service import APIKeyService

# Per-process, short-TTL cache of user records: user_id -> (user_dict,
# expires_at_monotonic). Every authenticated request was doing a fresh
# SQLite read here even when the API key itself was already cached —
# same trade-off as APIKeyService's validation cache: a role change or
# deactivation can take up to this long to take effect for a user
# already mid-session. routes/auth.py calls invalidate_user_cache()
# after update_user_role/delete to shrink that window in practice.
_USER_CACHE: dict = {}
_USER_CACHE_TTL_SECONDS = 30.0


def invalidate_user_cache(user_id: str) -> None:
    """Evict a cached user record, e.g. after a role change or deletion."""
    _USER_CACHE.pop(user_id, None)


def _get_cached_user(user_id: str) -> dict | None:
    cached = _USER_CACHE.get(user_id)
    if cached is None:
        return None
    user, expires_at = cached
    if time.monotonic() >= expires_at:
        del _USER_CACHE[user_id]
        return None
    return user


def _cache_user(user_id: str, user: dict) -> None:
    _USER_CACHE[user_id] = (user, time.monotonic() + _USER_CACHE_TTL_SECONDS)


# Routes reachable with no API key at all. This must stay a short,
# explicit allowlist: everything else under /api requires a key.
# Without this, a brand-new user has no way to register or log in,
# since both endpoints previously sat behind the same "/api" prefix
# check as the protected data routes.
#
# /debug and /debug/ (the performance dashboard's HTML shell — see
# core.performance.dashboard.live) are included for a different reason:
# a browser navigating there has no way to attach an x-api-key/Bearer
# header, so if this page were behind the same wall as its own data,
# nobody could ever load it to see the API-key input box that lets them
# authenticate the page's own fetch() calls. The shell page contains no
# data itself (just static HTML/CSS/JS); /debug/performance and
# /debug/request/{id}, the endpoints that actually return data, are
# NOT exempted — see install_performance_dashboard's per-route
# `dependencies`.
PUBLIC_PATHS = {
    "/api/auth/users/register",
    "/api/auth/users/login",
    "/debug",
    "/debug/",
}


def install_auth_middleware(app: FastAPI) -> None:
    """Register the auth-enforcement middleware on `app`."""

    @app.middleware("http")
    async def enforce_auth(request: Request, call_next):
        """Enforce API-key or JWT access on protected routes.

        `/debug` (the read-only `core.performance` dashboard installed
        by `core.app.api.app.create_app`, see
        `docs/performance/collectors-exporters-dashboard.md`) is
        included here alongside `/api` — without it, that dashboard's
        `dependencies=[Depends(require_admin_user)]` gate would never
        see a caller's role at all (this middleware is the only thing
        that ever populates `request.state.user_id`/`.role`), so it
        would 403 every request, admin or not, rather than actually
        enforcing "admin only".
        """
        if (
            (
                request.url.path.startswith("/api")
                or request.url.path.startswith("/debug")
            )
            and not request.url.path.startswith("/docs")
            and request.url.path not in PUBLIC_PATHS
        ):
            raw_api_key = (request.headers.get("x-api-key") or "").strip()
            auth_header = request.headers.get("authorization", "")
            bearer_token = (
                auth_header.replace("Bearer ", "", 1).strip() if auth_header else ""
            )

            service_manager = getattr(app.state, "service_manager", None)
            if service_manager is None:
                response = Response(status_code=HTTP_401_UNAUTHORIZED)
                response.headers["www-authenticate"] = "Bearer"
                return response

            authenticated = False

            # 1) An explicit x-api-key is the normal, long-lived credential
            # for machine/service callers.
            if raw_api_key:
                api_key_service = APIKeyService(service_manager)
                validated_key = await api_key_service.validate_api_key(raw_api_key)
                if validated_key:
                    owner_id = validated_key.get("owner_id", "")
                    request.state.user_id = owner_id
                    request.state.username = ""
                    request.state.role = ""
                    # API keys carry their own scopes (e.g. "read,write"),
                    # independent of the owning user's role -- see
                    # core.db.sql_policy.has_scope for how routes use
                    # this to gate mutating statements.
                    request.state.scopes = validated_key.get("scopes", "") or ""
                    request.state.session_id = (
                        request.headers.get("x-session-id", "") or "api-key"
                    )

                    if owner_id and hasattr(service_manager, "users"):
                        user = _get_cached_user(owner_id)
                        if user is None:
                            user = await run_in_service_executor(
                                service_manager.users.get_by_id, owner_id
                            )
                            if user:
                                _cache_user(owner_id, user)
                        if user:
                            request.state.username = user.get("username", "")
                            roles_val = user.get("roles") or user.get("role") or ""
                            request.state.role = (
                                roles_val.split(",")[0]
                                if isinstance(roles_val, str) and roles_val
                                else ""
                            )
                    authenticated = True

            # 2) Otherwise, accept the JWT issued at /api/auth/users/login as
            # `Authorization: Bearer <token>`. Without this branch there is
            # no way for a freshly registered/logged-in user to authenticate
            # *any* request — including POST /api/auth/keys, the endpoint
            # that mints their first x-api-key. The JWT is self-contained
            # (signed with settings.jwt_secret_key), so this doesn't need a
            # service_manager lookup the way the api-key path does; the
            # trade-off is that it's valid for anything under /api, not just
            # key creation, until it expires (settings.jwt_expiry_seconds).
            if not authenticated and bearer_token:
                settings = getattr(app.state, "settings", None)
                if settings:
                    try:
                        payload = jwt.decode(
                            bearer_token,
                            settings.jwt_secret_key,
                            algorithms=[settings.jwt_algorithm],
                        )
                    except jwt.PyJWTError:
                        payload = None

                    if payload:
                        request.state.user_id = payload.get("user_id", "")
                        request.state.username = payload.get("username", "")
                        request.state.role = payload.get("role", "")
                        # JWT sessions have no independent scopes concept
                        # (unlike API keys) -- derive an equivalent from
                        # role instead: admins get full access, everyone
                        # else is read-only until they mint a
                        # write-scoped API key. See core.db.sql_policy.
                        request.state.scopes = (
                            "read,write"
                            if payload.get("role") == "admin"
                            else "read"
                        )
                        request.state.session_id = (
                            request.headers.get("x-session-id", "") or "jwt"
                        )
                        authenticated = True

            if not authenticated:
                response = Response(status_code=HTTP_401_UNAUTHORIZED)
                response.headers["www-authenticate"] = "Bearer"
                return response

        return await call_next(request)
