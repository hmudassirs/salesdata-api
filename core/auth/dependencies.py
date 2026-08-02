"""Auth-specific FastAPI dependency.

Extracted from core/app/api/dependencies.py, which mixed this
auth-specific dependency in with generic DI (GetDB, GetServiceManager,
GetSettings). CurrentUser belongs with the rest of auth/, not with
generic app wiring.
"""

from dataclasses import dataclass

from fastapi import Depends, HTTPException, Request


@dataclass(frozen=True)
class CurrentUser:
    """Identity of the caller, as resolved by the API-key/JWT middleware
    in core.auth.middleware.

    Bundles `user_id`, `role`, and `scopes` together so routes depend on
    one typed object instead of pulling loose values off `request.state`
    by name.
    """

    user_id: str
    role: str
    scopes: str = ""

    @property
    def is_admin(self) -> bool:
        return self.role == "admin"


def get_current_user(request: Request) -> CurrentUser:
    """Return the caller's identity, set on request.state by
    core.auth.middleware (`request.state.user_id` / `.role` / `.scopes`).

    Routes should authorize against this instead of trusting a
    client-supplied owner_id/user_id in the path or body.
    """
    return CurrentUser(
        user_id=getattr(request.state, "user_id", "") or "",
        role=getattr(request.state, "role", "") or "",
        scopes=getattr(request.state, "scopes", "") or "",
    )


GetCurrentUser = Depends(get_current_user)


def require_admin_user(current_user: CurrentUser = GetCurrentUser) -> None:
    """FastAPI dependency: 403s any request whose caller isn't an admin.

    A public counterpart to `routes.auth._require_admin` (which is
    called explicitly inside a route body, not usable as a `Depends(...)`
    itself) for other routers — e.g.
    `core.performance.dashboard.install_performance_dashboard`'s
    `dependencies=[Depends(require_admin_user)]` — that want the same
    admin-only gate without importing a private route helper.
    """
    if not current_user.is_admin:
        raise HTTPException(status_code=403, detail="Admin privileges required")


RequireAdminUser = Depends(require_admin_user)
