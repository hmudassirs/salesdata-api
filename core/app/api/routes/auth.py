"""Auth routes: user registration/login, API key management.

Split out from the old combined routes.py, which mixed data-query
routes and auth routes in one 757-line file under the same module —
despite already having a separate `auth_router` for these, they lived
in the same file as /api/query and friends.
"""

import secrets
import time

import jwt
from fastapi import APIRouter, HTTPException, Request

from core.app.api.dependencies import (
    CurrentUser,
    GetCurrentUser,
    GetServiceManager,
    GetSettings,
)
from core.app.settings import AppSettings
from core.auth.api_key_service import APIKeyService
from core.auth.middleware import invalidate_user_cache
from core.auth.models import (
    APIKeyCreate,
    APIKeyDeleteResponse,
    APIKeyListResponse,
    APIKeyResponse,
    APIKeyRevokeResponse,
    AuthResponse,
    UserCreate,
    UserListResponse,
    UserLogin,
    UserResponse,
    UserRoleUpdate,
)
from core.auth.passwords import hash_password, verify_password
from core.observability.context import build_request_context

auth_router = APIRouter(prefix="/api/auth", tags=["authentication"])


def _require_owner_or_admin(current_user: CurrentUser, owner_id: str) -> None:
    """Authorize a request against a resource's owner_id.

    Enforces "you can only act on your own resources, unless you're an
    admin" — previously any valid API key could list/revoke/delete
    *another user's* keys by supplying a different owner_id.

    Raises:
        HTTPException: 403 if the caller is neither the resource owner
            nor an admin.
    """
    if current_user.is_admin:
        return
    if not current_user.user_id or current_user.user_id != owner_id:
        raise HTTPException(
            status_code=403,
            detail="Not authorized to access this resource",
        )


def _require_admin(current_user: CurrentUser) -> None:
    """Authorize a request as admin-only.

    Raises:
        HTTPException: 403 if the caller's role is not "admin".
    """
    if not current_user.is_admin:
        raise HTTPException(status_code=403, detail="Admin privileges required")


def _primary_role(user: dict) -> str:
    """Normalize a user record's role into a single primary role string.

    Role may be stored under either `roles` (comma-separated) or `role`
    depending on schema version; this picks the first value present and
    falls back to "user".
    """
    roles_val = user.get("roles") or user.get("role") or ""
    if isinstance(roles_val, str) and roles_val:
        return roles_val.split(",")[0]
    return "user"


@auth_router.post("/keys", response_model=APIKeyResponse)
async def create_api_key(
    request: Request,
    key_data: APIKeyCreate,
    service_manager=GetServiceManager,
    current_user: CurrentUser = GetCurrentUser,
) -> APIKeyResponse:
    """Create a new API key for a user.

    A caller may create a key for themselves; only an admin may create
    a key on behalf of a different owner_id.

    Args:
        key_data: APIKeyCreate with owner_id and optional scopes

    Returns:
        APIKeyResponse with the raw API key (shown only once)
    """
    try:
        build_request_context(request)
        _require_owner_or_admin(current_user, key_data.owner_id)
        api_key_service = APIKeyService(service_manager)

        result = await api_key_service.create_api_key(
            owner_id=key_data.owner_id,
            scopes=key_data.scopes,
            expires_at=key_data.expires_at,
        )

        # Log API key creation
        service_manager.audit.log_audit_event(
            event_type="api_key.create",
            user_id=key_data.owner_id,
            resource_type="api_key",
            resource_id=result["key_id"],
            action="create",
            success=True,
            metadata={"scopes": key_data.scopes, "expires_at": key_data.expires_at},
        )

        return APIKeyResponse(**result)
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(
            status_code=400, detail=f"Failed to create API key: {str(e)}"
        )


@auth_router.get("/keys/{owner_id}", response_model=APIKeyListResponse)
async def list_api_keys(
    request: Request,
    owner_id: str,
    service_manager=GetServiceManager,
    current_user: CurrentUser = GetCurrentUser,
) -> APIKeyListResponse:
    """List all API keys for a user.

    Args:
        owner_id: User ID to list keys for

    Returns:
        APIKeyListResponse with all non-revoked keys
    """
    try:
        build_request_context(request)
        _require_owner_or_admin(current_user, owner_id)
        api_key_service = APIKeyService(service_manager)

        keys = await api_key_service.list_api_keys(owner_id)

        return APIKeyListResponse(
            keys=keys,
            count=len(keys),
        )
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(
            status_code=400, detail=f"Failed to list API keys: {str(e)}"
        )


@auth_router.post("/keys/{key_id}/revoke", response_model=APIKeyRevokeResponse)
async def revoke_api_key(
    request: Request,
    key_id: str,
    owner_id: str,
    service_manager=GetServiceManager,
    current_user: CurrentUser = GetCurrentUser,
) -> APIKeyRevokeResponse:
    """Revoke an API key (disable without deleting).

    Args:
        key_id: ID of the key to revoke
        owner_id: User ID who owns the key (for authorization)

    Returns:
        APIKeyRevokeResponse with success status
    """
    try:
        build_request_context(request)
        _require_owner_or_admin(current_user, owner_id)
        api_key_service = APIKeyService(service_manager)

        await api_key_service.revoke_api_key(key_id, owner_id)

        # Log API key revocation
        service_manager.audit.log_audit_event(
            event_type="api_key.revoke",
            user_id=owner_id,
            resource_type="api_key",
            resource_id=key_id,
            action="revoke",
            success=True,
        )

        return APIKeyRevokeResponse(
            key_id=key_id,
            message="API key revoked successfully",
        )
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(
            status_code=400, detail=f"Failed to revoke API key: {str(e)}"
        )


@auth_router.delete("/keys/{key_id}", response_model=APIKeyDeleteResponse)
async def delete_api_key(
    request: Request,
    key_id: str,
    owner_id: str,
    service_manager=GetServiceManager,
    current_user: CurrentUser = GetCurrentUser,
) -> APIKeyDeleteResponse:
    """Delete an API key permanently.

    Args:
        key_id: ID of the key to delete
        owner_id: User ID who owns the key (for authorization)

    Returns:
        APIKeyDeleteResponse with success status
    """
    try:
        build_request_context(request)
        _require_owner_or_admin(current_user, owner_id)
        api_key_service = APIKeyService(service_manager)

        await api_key_service.delete_api_key(key_id, owner_id)

        # Log API key deletion
        service_manager.audit.log_audit_event(
            event_type="api_key.delete",
            user_id=owner_id,
            resource_type="api_key",
            resource_id=key_id,
            action="delete",
            success=True,
        )

        return APIKeyDeleteResponse(
            key_id=key_id,
            message="API key deleted successfully",
        )
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(
            status_code=400, detail=f"Failed to delete API key: {str(e)}"
        )


# ============= USER MANAGEMENT ENDPOINTS =============


@auth_router.post("/users/register", response_model=UserResponse)
async def register_user(
    request: Request,
    user_data: UserCreate,
    service_manager=GetServiceManager,
) -> UserResponse:
    """Register a new user.

    Args:
        user_data: UserCreate with username, email, password, and role

    Returns:
        UserResponse with created user data
    """
    try:
        build_request_context(request)

        password_hash = hash_password(user_data.password)
        user_id = f"user_{int(time.time())}_{secrets.token_hex(4)}"

        # Self-service registration always creates a "user" account.
        # user_data.role is intentionally ignored here — honoring a
        # client-supplied role would let anyone register as "admin"
        # directly, since this endpoint has no auth requirement.
        # Role elevation must go through a separate admin-only endpoint.
        fixed_role = "user"

        # Create user
        success = service_manager.users.create(
            user_id=user_id,
            username=user_data.username,
            email=user_data.email,
            password_hash=password_hash,
            role=fixed_role,
        )

        if not success:
            raise HTTPException(status_code=400, detail="Failed to create user")

        # Log user registration
        service_manager.audit.log_audit_event(
            event_type="user.register",
            user_id=user_id,
            resource_type="user",
            resource_id=user_id,
            action="create",
            success=True,
            metadata={"username": user_data.username, "email": user_data.email},
        )

        return UserResponse(
            user_id=user_id,
            username=user_data.username,
            email=user_data.email,
            role=fixed_role,
            created_at=int(time.time()),
            is_active=True,
        )
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(
            status_code=400, detail=f"Failed to register user: {str(e)}"
        )


@auth_router.post("/users/login", response_model=AuthResponse)
async def login_user(
    request: Request,
    login_data: UserLogin,
    service_manager=GetServiceManager,
    settings: AppSettings = GetSettings,
) -> AuthResponse:
    """Authenticate a user.

    Args:
        login_data: UserLogin with username and password

    Returns:
        AuthResponse with user data and token
    """
    try:
        build_request_context(request)

        # Get user by username
        user = service_manager.users.get_by_username(login_data.username)
        if not user or not verify_password(
            login_data.password, user.get("password_hash", "")
        ):
            # Log failed login attempt
            service_manager.audit.log_audit_event(
                event_type="user.login",
                user_id=user.get("user_id") if user else "unknown",
                resource_type="user",
                resource_id=user.get("user_id") if user else "unknown",
                action="login",
                success=False,
                error_message="Invalid credentials",
                ip_address=request.client.host if request.client else "unknown",
            )
            return AuthResponse(
                success=False,
                user=None,
                token=None,
                message="Invalid username or password",
            )

        if not user.get("is_active"):
            return AuthResponse(
                success=False, user=None, token=None, message="Account is disabled"
            )

        # Normalize role(s) stored as `roles` in DB (comma-separated)
        primary_role = _primary_role(user)

        token_payload = {
            "user_id": user["user_id"],
            "username": user["username"],
            "role": primary_role,
            "exp": int(time.time()) + settings.jwt_expiry_seconds,
        }
        token = jwt.encode(
            token_payload, settings.jwt_secret_key, algorithm=settings.jwt_algorithm
        )

        # Update last login
        service_manager.users.update_last_login(user["user_id"])

        # Log successful login
        service_manager.audit.log_audit_event(
            event_type="user.login",
            user_id=user["user_id"],
            resource_type="user",
            resource_id=user["user_id"],
            action="login",
            success=True,
            ip_address=request.client.host if request.client else "unknown",
        )

        return AuthResponse(
            success=True,
            user=UserResponse(
                user_id=user["user_id"],
                username=user["username"],
                email=user["email"],
                role=primary_role,
                created_at=user["created_at"],
                is_active=user["is_active"],
            ),
            token=token,
            message="Login successful",
        )
    except Exception as e:
        raise HTTPException(
            status_code=400, detail=f"Failed to authenticate user: {str(e)}"
        )


@auth_router.get("/users", response_model=UserListResponse)
async def list_users(
    request: Request,
    service_manager=GetServiceManager,
    current_user: CurrentUser = GetCurrentUser,
) -> UserListResponse:
    """List all users (admin only).

    Returns:
        UserListResponse with all users
    """
    try:
        build_request_context(request)
        _require_admin(current_user)

        users_data = service_manager.users.list_all()
        users = []
        for user in users_data:
            users.append(
                UserResponse(
                    user_id=user["user_id"],
                    username=user["username"],
                    email=user["email"],
                    role=_primary_role(user),
                    created_at=user["created_at"],
                    is_active=user["is_active"],
                )
            )

        return UserListResponse(
            users=users,
            count=len(users),
        )
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Failed to list users: {str(e)}")


@auth_router.patch("/users/{user_id}/role", response_model=UserResponse)
async def update_user_role(
    request: Request,
    user_id: str,
    role_update: UserRoleUpdate,
    service_manager=GetServiceManager,
    current_user: CurrentUser = GetCurrentUser,
) -> UserResponse:
    """Update a user's role (admin only).

    This is the only supported way to grant/revoke admin — self-service
    registration always creates a plain "user" account (see
    register_user), so promoting someone requires an existing admin to
    call this endpoint.

    Args:
        user_id: ID of the user whose role is being changed
        role_update: UserRoleUpdate with the new role

    Returns:
        UserResponse reflecting the updated role
    """
    try:
        build_request_context(request)
        _require_admin(current_user)

        target_user = service_manager.users.get_by_id(user_id)
        if not target_user:
            raise HTTPException(status_code=404, detail=f"User not found: {user_id}")

        previous_role = _primary_role(target_user)

        # Guard against locking everyone out of admin: if this would
        # demote the last remaining admin, refuse rather than silently
        # leaving the system with no one who can grant admin back.
        if previous_role == "admin" and role_update.role != "admin":
            all_users = service_manager.users.list_all()
            remaining_admins = [
                u
                for u in all_users
                if u.get("user_id") != user_id and _primary_role(u) == "admin"
            ]
            if not remaining_admins:
                raise HTTPException(
                    status_code=409,
                    detail="Cannot demote the last remaining admin",
                )

        success = service_manager.users.update_role(user_id, role_update.role)
        if not success:
            raise HTTPException(status_code=400, detail="Failed to update user role")

        # Shrink the cache staleness window: without this, a just-promoted
        # (or demoted) user's role stays as the middleware last cached it
        # for up to the cache's TTL.
        invalidate_user_cache(user_id)

        service_manager.audit.log_audit_event(
            event_type="user.role_update",
            user_id=current_user.user_id,
            resource_type="user",
            resource_id=user_id,
            action="update_role",
            success=True,
            metadata={"previous_role": previous_role, "new_role": role_update.role},
        )

        updated_user = service_manager.users.get_by_id(user_id) or target_user

        return UserResponse(
            user_id=updated_user["user_id"],
            username=updated_user["username"],
            email=updated_user["email"],
            role=_primary_role(updated_user) or role_update.role,
            created_at=updated_user["created_at"],
            is_active=updated_user["is_active"],
        )
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(
            status_code=400, detail=f"Failed to update user role: {str(e)}"
        )
