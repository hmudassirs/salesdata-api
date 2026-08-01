# core/auth/models.py
"""Authentication models."""

import time
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator


class APIKeyCreate(BaseModel):
    """Model for creating an API key."""

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "owner_id": "user_admin_001",
                "scopes": "read,write",
                "expires_at": None,
            }
        }
    )

    owner_id: str = Field(..., description="User ID who owns the key")
    scopes: Optional[str] = Field(
        None,
        description="Comma-separated scopes (e.g., 'read,write')",
    )
    expires_at: Optional[int] = Field(
        None,
        description="Unix timestamp when key expires (must be in the future)",
    )

    @field_validator("expires_at")
    @classmethod
    def _expires_at_must_be_future(cls, value: Optional[int]) -> Optional[int]:
        if value is not None and value <= int(time.time()):
            raise ValueError("expires_at must be a future Unix timestamp")
        return value


class APIKeyResponse(BaseModel):
    """Model for API key response (shown once on creation)."""

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "key_id": "key_1707000000_a1b2c3d4",
                "api_key": "example_raw_key_xyz123abc",
                "owner_id": "user_admin_001",
                "created_at": 1707000000,
                "expires_at": None,
                "scopes": "read,write",
                "is_active": True,
            }
        }
    )

    key_id: str = Field(..., description="Unique key identifier")
    api_key: str = Field(..., description="Raw API key (shown only once)")
    owner_id: str = Field(..., description="User ID who owns the key")
    created_at: int = Field(..., description="Unix timestamp when created")
    expires_at: Optional[int] = Field(None, description="Unix timestamp when expires")
    scopes: Optional[str] = Field(None, description="Granted scopes")
    is_active: bool = Field(..., description="Whether key is active")


class APIKeyInfo(BaseModel):
    """Model for API key info (without raw key)."""

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "key_id": "key_1706999999_a1b2c3d4",
                "owner_id": "user_admin_000",
                "created_at": 1706999999,
                "expires_at": None,
                "scopes": "read,write",
                "is_active": True,
            }
        }
    )

    key_id: str = Field(..., description="Unique key identifier")
    owner_id: str = Field(..., description="User ID who owns the key")
    created_at: int = Field(..., description="Unix timestamp when created")
    expires_at: Optional[int] = Field(None, description="Unix timestamp when expires")
    scopes: Optional[str] = Field(None, description="Granted scopes")
    is_active: bool = Field(..., description="Whether key is active")


class APIKeyListResponse(BaseModel):
    """Model for listing API keys."""

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "keys": [
                    {
                        "key_id": "key_1707000000_a1b2c3d4",
                        "owner_id": "user_admin_001",
                        "created_at": 1707000000,
                        "expires_at": None,
                        "scopes": "read,write",
                        "is_active": True,
                    }
                ],
                "count": 1,
            }
        }
    )

    keys: list[APIKeyInfo] = Field(..., description="List of API keys")
    count: int = Field(..., description="Number of keys")


class APIKeyRevokeResponse(BaseModel):
    """Model for revoking an API key."""

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "key_id": "key_1707000000_a1b2c3d4",
                "message": "API key revoked successfully",
            }
        }
    )

    key_id: str = Field(..., description="Key ID that was revoked")
    message: str = Field(..., description="Status message")


class APIKeyDeleteResponse(BaseModel):
    """Model for deleting an API key."""

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "key_id": "key_1707000000_a1b2c3d4",
                "message": "API key deleted successfully",
            }
        }
    )
    key_id: str = Field(..., description="Key ID that was deleted")
    message: str = Field(..., description="Status message")


class UserCreate(BaseModel):
    """Model for creating a user."""

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "username": "johndoe",
                "email": "john@example.com",
                "password": "securepassword123",
                "role": "user",
            }
        }
    )
    username: str = Field(..., description="Unique username")
    email: str = Field(..., description="User email address")
    password: str = Field(
        ..., min_length=8, description="User password (min. 8 characters)"
    )
    role: str = Field(
        "user",
        description=(
            "Requested role. Ignored for self-service registration, which always "
            "creates a 'user' account — elevation to other roles must go through "
            "an admin-only endpoint, not this field."
        ),
    )


class UserRoleUpdate(BaseModel):
    """Model for updating a user's role (admin only)."""

    model_config = ConfigDict(json_schema_extra={"example": {"role": "admin"}})

    role: str = Field(..., description="New role to assign")

    @field_validator("role")
    @classmethod
    def _role_must_be_known(cls, value: str) -> str:
        # Restrict to a known set rather than accepting any string: a
        # typo'd role (e.g. "admni") would silently create a user with
        # no matching permissions anywhere in the app, which is a
        # confusing way to fail.
        allowed = {"user", "admin"}
        if value not in allowed:
            raise ValueError(f"role must be one of {sorted(allowed)}")
        return value


class UserLogin(BaseModel):
    """Model for user login."""

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "username": "johndoe",
                "password": "securepassword123",
            }
        }
    )
    username: str = Field(..., description="Username")
    password: str = Field(..., description="Password")


class UserResponse(BaseModel):
    """Model for user response."""

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "user_id": "user_1707000000_a1b2c3d4",
                "username": "johndoe",
                "email": "john@example.com",
                "role": "user",
                "created_at": 1707000000,
                "is_active": True,
            }
        }
    )

    user_id: str = Field(..., description="Unique user identifier")
    username: str = Field(..., description="Username")
    email: str = Field(..., description="Email address")
    role: str = Field(..., description="User role")
    created_at: int = Field(..., description="Unix timestamp when created")
    is_active: bool = Field(..., description="Whether user is active")


class UserListResponse(BaseModel):
    """Model for listing users."""

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "users": [
                    {
                        "user_id": "user_1707000000_a1b2c3d4",
                        "username": "johndoe",
                        "email": "john@example.com",
                        "role": "user",
                        "created_at": 1707000000,
                        "is_active": True,
                    }
                ],
                "count": 1,
            }
        }
    )

    users: list[UserResponse] = Field(..., description="List of users")
    count: int = Field(..., description="Total number of users")


class AuthResponse(BaseModel):
    """Model for authentication response."""

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "success": True,
                "user": {
                    "user_id": "user_1707000000_a1b2c3d4",
                    "username": "johndoe",
                    "email": "john@example.com",
                    "role": "user",
                    "created_at": 1707000000,
                    "is_active": True,
                },
                "token": "eyJ0eXAiOiJKV1QiLCJhbGciOiJIUzI1NiJ9...",
                "message": "Login successful",
            }
        }
    )

    success: bool = Field(..., description="Whether authentication succeeded")
    user: Optional[UserResponse] = Field(None, description="User data if authenticated")
    token: Optional[str] = Field(None, description="JWT token if authenticated")
    message: str = Field(..., description="Status message")
