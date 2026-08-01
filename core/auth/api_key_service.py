"""API key management service (business logic: generate/hash keys,
call the repository layer). Renamed file from `service.py` — that name
gave no indication this was about API keys specifically, and it sat in
core/auth/ alongside models.py/passwords.py with no way to tell which
file did what without opening each one."""

import asyncio
import hashlib
import secrets
import time
from typing import Optional

from core.db.logger import get_logger
from core.service_registry import ServiceManager

logger = get_logger(__name__)


class APIKeyService:
    """Service for managing API keys.

    Every call into `self.service_manager.api_keys` here is offloaded via
    `asyncio.to_thread` — those calls are blocking SQLite I/O underneath
    (see core/storage/service_db.py), and this class is always called
    from async request-handling code (the auth middleware, in particular,
    on literally every request). Calling them directly would block the
    whole event loop for the duration of each SQLite round-trip — this
    was a real, measured bottleneck earlier in this codebase's history
    (500 concurrent requests collapsing to ~8 req/s) before being fixed.
    """

    # Per-process, short-TTL cache of validated keys: api_key_hash ->
    # (validated_dict, expires_at_monotonic). Avoids a SQLite round-trip
    # on every request from a client reusing the same key repeatedly.
    # TTL bounds the window in which a revoked/deleted key would still
    # authenticate — keep this short (seconds, not minutes); the goal is
    # collapsing repeated validation of the *same* key under load, not
    # making revocation slow. Per-process: if you ever run multiple
    # uvicorn workers or instances, each has its own independent cache,
    # so a revoke isn't instantly consistent across all of them either —
    # only a shared cache (e.g. Redis) would fix that.
    # Not lock-protected: dict get/set is atomic enough under the GIL,
    # and the worst case of a rare race is one redundant DB lookup, not
    # a correctness problem.
    _validation_cache: dict = {}
    _CACHE_TTL_SECONDS = 30.0

    def __init__(self, service_manager: ServiceManager):
        """Initialize API key service.

        Args:
            service_manager: Service manager instance for auxiliary data
        """
        self.service_manager = service_manager

    @staticmethod
    def generate_api_key() -> str:
        """Generate a random API key.

        Returns:
            Random 32-character API key
        """
        return secrets.token_urlsafe(32)

    @staticmethod
    def hash_api_key(api_key: str) -> str:
        """Hash an API key using SHA256.

        Args:
            api_key: The raw API key

        Returns:
            Hashed API key (hex digest)
        """
        return hashlib.sha256(api_key.encode()).hexdigest()

    async def create_api_key(
        self,
        owner_id: str,
        scopes: Optional[str] = None,
        expires_at: Optional[int] = None,
    ) -> dict:
        """Create a new API key for a user.

        Args:
            owner_id: User ID who owns the key
            scopes: Comma-separated scopes (e.g., "read,write")
            expires_at: Unix timestamp when key expires (None = never)

        Returns:
            Dictionary with key_id, api_key (raw, shown once), and metadata
        """
        api_key = self.generate_api_key()
        api_key_hash = self.hash_api_key(api_key)
        key_id = f"key_{int(time.time())}_{secrets.token_hex(4)}"
        created_at = int(time.time())

        try:
            await asyncio.to_thread(
                self.service_manager.api_keys.create,
                key_id=key_id,
                api_key_hash=api_key_hash,
                owner_id=owner_id,
                created_at=created_at,
                expires_at=expires_at,
                scopes=scopes,
                is_active=True,
            )
            logger.info(f"Created API key {key_id} for user {owner_id}")

            return {
                "key_id": key_id,
                "api_key": api_key,  # Show only once
                "owner_id": owner_id,
                "created_at": created_at,
                "expires_at": expires_at,
                "scopes": scopes,
                "is_active": True,
            }
        except Exception as e:
            logger.error(f"Error creating API key: {e}")
            raise

    async def validate_api_key(self, api_key: str) -> Optional[dict]:
        """Validate an API key and return its metadata.

        Cached for `_CACHE_TTL_SECONDS`: a revoked key can remain valid
        for up to that long after revocation. See the class docstring
        for the full trade-off.

        Args:
            api_key: The raw API key to validate

        Returns:
            API key metadata if valid, None otherwise
        """
        api_key_hash = self.hash_api_key(api_key)
        now_monotonic = time.monotonic()

        cached = self._validation_cache.get(api_key_hash)
        if cached is not None:
            validated, expires_at = cached
            if now_monotonic < expires_at:
                return validated
            del self._validation_cache[api_key_hash]

        now = int(time.time())

        try:
            result = await asyncio.to_thread(
                self.service_manager.api_keys.validate, api_key_hash, now
            )
            if not result:
                return None

            validated = {
                "key_id": result["key_id"],
                "owner_id": result["owner_id"],
                "created_at": result["created_at"],
                "expires_at": result["expires_at"],
                "scopes": result["scopes"],
                "is_active": result["is_active"],
            }
            self._validation_cache[api_key_hash] = (
                validated,
                now_monotonic + self._CACHE_TTL_SECONDS,
            )
            return validated
        except Exception as e:
            logger.error(f"Error validating API key: {e}")
            return None

    async def list_api_keys(self, owner_id: str) -> list[dict]:
        """List all API keys for a user (without showing raw keys).

        Args:
            owner_id: User ID to list keys for

        Returns:
            List of API key metadata (hashed keys)
        """
        try:
            results = await asyncio.to_thread(
                self.service_manager.api_keys.list_by_owner, owner_id
            )
            return [
                {
                    "key_id": row["key_id"],
                    "owner_id": row["owner_id"],
                    "created_at": row["created_at"],
                    "expires_at": row["expires_at"],
                    "scopes": row["scopes"],
                    "is_active": row["is_active"],
                }
                for row in results
            ]
        except Exception as e:
            logger.error(f"Error listing API keys: {e}")
            raise

    async def revoke_api_key(self, key_id: str, owner_id: str) -> bool:
        """Revoke an API key.

        Args:
            key_id: ID of the key to revoke
            owner_id: Owner user ID (for authorization check)

        Returns:
            True if revoked, False if not found
        """
        try:
            result = await asyncio.to_thread(
                self.service_manager.api_keys.revoke, key_id, owner_id
            )
            if result:
                logger.info(f"Revoked API key {key_id}")
                self._evict_by_key_id(key_id)
            return result
        except Exception as e:
            logger.error(f"Error revoking API key: {e}")
            raise

    async def delete_api_key(self, key_id: str, owner_id: str) -> bool:
        """Delete an API key permanently.

        Args:
            key_id: ID of the key to delete
            owner_id: Owner user ID (for authorization check)

        Returns:
            True if deleted, False if not found
        """
        try:
            result = await asyncio.to_thread(
                self.service_manager.api_keys.delete, key_id, owner_id
            )
            if result:
                logger.info(f"Deleted API key {key_id}")
                self._evict_by_key_id(key_id)
            return result
        except Exception as e:
            logger.error(f"Error deleting API key: {e}")
            raise

    def _evict_by_key_id(self, key_id: str) -> None:
        """Best-effort cache eviction on revoke/delete.

        The validation cache is keyed by api_key_hash, but revoke/delete
        only know key_id — we don't have the raw key (it's never stored)
        to re-derive the hash. So this scans cached entries' key_id field
        instead of doing an O(1) lookup. Fine at this scale (the cache
        only ever holds recently-used keys, not the whole table) but
        worth knowing it's O(n) in cache size, not O(1).
        """
        stale = [
            h
            for h, (validated, _) in self._validation_cache.items()
            if validated.get("key_id") == key_id
        ]
        for h in stale:
            del self._validation_cache[h]
