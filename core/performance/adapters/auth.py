"""Timing wrappers around `core.auth`'s API-key, JWT, and authorization checks.

Mirrors the shape of `adapters.pool`: wrap the existing object behind
the same public interface and delegate every call unchanged, adding
timing only when a profiler is bound to the current context.

Three seams are covered, matching where real work (and therefore real
cost) happens in `core.auth`:

- `InstrumentedAPIKeyService` wraps `core.auth.api_key_service.APIKeyService`.
  `validate_api_key` in particular runs on every authenticated request
  (see that class's own docstring), offloading a SQLite round-trip via
  `asyncio.to_thread` on a cache miss — exactly the kind of per-request
  cost this subsystem exists to make visible.
- `instrumented_jwt_decode` wraps `jwt.decode`, the bearer-token path
  used by `core.auth.middleware.enforce_auth` as a fallback when no
  `x-api-key` header is present.
- `authorize` is a small helper that generalizes the
  "compute a boolean, raise if it's False" shape used by authorization
  checks (e.g. `core.app.api.routes.auth._require_admin`), timing the
  check itself under `AUTHORIZATION`. It is deliberately generic since
  this codebase's authorization logic is currently a synchronous,
  effectively-free role comparison with no I/O of its own to observe;
  the seam exists so any future authorization mechanism with real cost
  (a remote policy check, a scope lookup) can adopt it without a new
  adapter.

None of `core.auth.api_key_service`, `core.auth.middleware`, or
`core.app.api.routes.auth` are modified — adopting any of these is
opt-in at the call site.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, cast

import jwt

from core.auth.api_key_service import APIKeyService
from core.performance.context import get_current_profiler
from core.performance.enums import PerformanceStage
from core.performance.types import MetricName


class InstrumentedAPIKeyService:
    """Wrap `APIKeyService`, timing each operation under a matching stage.

    `validate_api_key` — the hot-path method — is timed under
    `API_KEY_LOOKUP`. The remaining CRUD operations (`create`, `list`,
    `revoke`, `delete`) are administrative rather than per-request, and
    there is no dedicated `PerformanceStage` member for API-key
    management writes, so they are timed under `CUSTOM` with a
    descriptive metric name rather than inventing an ad-hoc stage.
    """

    __slots__ = ("_service",)

    def __init__(self, service: APIKeyService) -> None:
        self._service = service

    @staticmethod
    def generate_api_key() -> str:
        """Delegate to `APIKeyService.generate_api_key`, untimed (pure, in-memory)."""
        return APIKeyService.generate_api_key()

    @staticmethod
    def hash_api_key(api_key: str) -> str:
        """Delegate to `APIKeyService.hash_api_key`, untimed (pure, in-memory)."""
        return APIKeyService.hash_api_key(api_key)

    async def validate_api_key(self, api_key: str) -> dict[str, Any] | None:
        """Validate `api_key`, timing the call under `API_KEY_LOOKUP`."""
        profiler = get_current_profiler()
        if profiler is None:
            return await self._service.validate_api_key(api_key)
        with profiler.stage(
            PerformanceStage.API_KEY_LOOKUP, MetricName("api_key_validate")
        ):
            return await self._service.validate_api_key(api_key)

    async def create_api_key(
        self,
        owner_id: str,
        scopes: str | None = None,
        expires_at: int | None = None,
    ) -> dict[str, Any]:
        """Create an API key, timing the call under `CUSTOM`."""
        profiler = get_current_profiler()
        if profiler is None:
            return await self._service.create_api_key(owner_id, scopes, expires_at)
        with profiler.stage(PerformanceStage.CUSTOM, MetricName("api_key_create")):
            return await self._service.create_api_key(owner_id, scopes, expires_at)

    async def list_api_keys(self, owner_id: str) -> list[dict[str, Any]]:
        """List a user's API keys, timing the call under `CUSTOM`."""
        profiler = get_current_profiler()
        if profiler is None:
            return await self._service.list_api_keys(owner_id)
        with profiler.stage(PerformanceStage.CUSTOM, MetricName("api_key_list")):
            return await self._service.list_api_keys(owner_id)

    async def revoke_api_key(self, key_id: str, owner_id: str) -> bool:
        """Revoke an API key, timing the call under `CUSTOM`."""
        profiler = get_current_profiler()
        if profiler is None:
            return await self._service.revoke_api_key(key_id, owner_id)
        with profiler.stage(PerformanceStage.CUSTOM, MetricName("api_key_revoke")):
            return await self._service.revoke_api_key(key_id, owner_id)

    async def delete_api_key(self, key_id: str, owner_id: str) -> bool:
        """Delete an API key, timing the call under `CUSTOM`."""
        profiler = get_current_profiler()
        if profiler is None:
            return await self._service.delete_api_key(key_id, owner_id)
        with profiler.stage(PerformanceStage.CUSTOM, MetricName("api_key_delete")):
            return await self._service.delete_api_key(key_id, owner_id)


def instrumented_jwt_decode(
    token: str, key: str, algorithms: list[str]
) -> dict[str, Any]:
    """Decode and verify a JWT, timing the call under `AUTHENTICATION`.

    A direct, drop-in wrapper around `jwt.decode`: same positional
    signature, same return value on success, and the same
    `jwt.PyJWTError` (and subclasses, e.g. `ExpiredSignatureError`) on
    failure — nothing is caught or translated here, matching
    `core.auth.middleware.enforce_auth`'s own `try/except jwt.PyJWTError`.
    """
    profiler = get_current_profiler()
    if profiler is None:
        return cast("dict[str, Any]", jwt.decode(token, key, algorithms=algorithms))
    with profiler.stage(PerformanceStage.AUTHENTICATION, MetricName("jwt_decode")):
        return cast("dict[str, Any]", jwt.decode(token, key, algorithms=algorithms))


def authorize(
    check: Callable[[], bool],
    *,
    name: MetricName,
    on_denied: Callable[[], BaseException],
) -> None:
    """Time an authorization predicate under `AUTHORIZATION`; raise if denied.

    Generalizes the "compute a boolean, raise if it's False" shape used
    by call-site checks such as
    `core.app.api.routes.auth._require_admin` (`if not
    current_user.is_admin: raise HTTPException(...)`). `on_denied` is
    called to construct the exception only when `check()` returns
    False, so callers keep full control over the exception type,
    status code, and message — this never invents its own error.
    """
    profiler = get_current_profiler()
    if profiler is None:
        allowed = check()
    else:
        with profiler.stage(PerformanceStage.AUTHORIZATION, name):
            allowed = check()
    if not allowed:
        raise on_denied()
