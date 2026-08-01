"""Data access for the api_keys table."""

import time
from typing import Any, Dict, List, Optional

from core.db.logger import get_logger
from core.storage.service_db import ServiceDatabase

logger = get_logger(__name__)


class APIKeyRepository:
    """Thin data-access layer over the api_keys table.

    Renamed from the old `APIKeyService` — that name collided with
    core.auth.api_key_service.APIKeyService, a completely different class
    (business logic: generates/hashes keys, calls *this* repository for
    the actual reads/writes). Same problem this whole restructure exists
    to fix, just one level deeper.
    """

    def __init__(self, service_db: ServiceDatabase):
        """Initialize API key service.

        Args:
            service_db: Service database instance
        """
        self.service_db = service_db

    def create(
        self,
        key_id: str,
        api_key_hash: str,
        owner_id: str,
        created_at: int,
        expires_at: Optional[int] = None,
        scopes: Optional[str] = None,
        is_active: bool = True,
    ) -> bool:
        """Create a new API key.

        Returns:
            True if created successfully
        """
        return self.service_db.create_api_key(
            key_id, api_key_hash, owner_id, created_at, expires_at, scopes, is_active
        )

    def validate(
        self, api_key_hash: str, current_time: int
    ) -> Optional[Dict[str, Any]]:
        """Validate an API key hash.

        Args:
            api_key_hash: Hashed API key
            current_time: Current timestamp

        Returns:
            API key data if valid, None otherwise
        """
        return self.service_db.validate_api_key(api_key_hash, current_time)

    def list_by_owner(self, owner_id: str) -> List[Dict[str, Any]]:
        """List all API keys for an owner.

        Args:
            owner_id: Owner user ID

        Returns:
            List of API key data
        """
        return self.service_db.list_api_keys_by_owner(owner_id)

    def revoke(self, key_id: str, owner_id: str) -> bool:
        """Revoke an API key.

        Args:
            key_id: API key ID
            owner_id: Owner user ID

        Returns:
            True if revoked successfully
        """
        return self.service_db.revoke_api_key(key_id, owner_id)

    def delete(self, key_id: str, owner_id: str) -> bool:
        """Delete an API key.

        Args:
            key_id: API key ID
            owner_id: Owner user ID

        Returns:
            True if deleted successfully
        """
        return self.service_db.delete_api_key(key_id, owner_id)
