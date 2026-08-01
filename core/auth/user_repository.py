"""Data access for the users table."""

import time
from typing import Any, Dict, List, Optional

from core.db.logger import get_logger
from core.storage.service_db import ServiceDatabase

logger = get_logger(__name__)


class UserRepository:
    """Thin data-access layer over the users table."""

    def __init__(self, service_db: ServiceDatabase):
        """Initialize user service.

        Args:
            service_db: Service database instance
        """
        self.service_db = service_db

    def create(
        self,
        user_id: str,
        username: str,
        email: str,
        password_hash: str,
        role: str = "user",
        created_at: Optional[int] = None,
    ) -> bool:
        """Create a new user.

        Returns:
            True if created successfully
        """
        if created_at is None:
            created_at = int(time.time())
        return self.service_db.create_user(
            user_id, username, email, password_hash, role, created_at
        )

    def get_by_id(self, user_id: str) -> Optional[Dict[str, Any]]:
        """Get user by ID.

        Args:
            user_id: User ID

        Returns:
            User data if found
        """
        return self.service_db.get_user_by_id(user_id)

    def get_by_username(self, username: str) -> Optional[Dict[str, Any]]:
        """Get user by username.

        Args:
            username: Username

        Returns:
            User data if found
        """
        return self.service_db.get_user_by_username(username)

    def update_role(self, user_id: str, role: str) -> bool:
        """Update user role.

        Args:
            user_id: User ID
            role: New role

        Returns:
            True if updated successfully
        """
        return self.service_db.update_user_role(user_id, role)

    def delete(self, user_id: str) -> bool:
        """Delete a user.

        Args:
            user_id: User ID

        Returns:
            True if deleted successfully
        """
        return self.service_db.delete_user(user_id)

    def update_last_login(self, user_id: str) -> bool:
        """Update user's last login timestamp.

        Args:
            user_id: User ID

        Returns:
            True if updated successfully
        """
        sql = "UPDATE users SET last_login_at = ? WHERE user_id = ?"
        try:
            self.service_db.execute(sql, (int(time.time()), user_id))
            return True
        except Exception as e:
            logger.error(f"Failed to update last login: {e}")
            return False

    def list_all(self) -> List[Dict[str, Any]]:
        """List all users.

        Returns:
            List of all users
        """
        sql = "SELECT * FROM users ORDER BY created_at DESC"
        try:
            results = self.service_db.fetch_all(sql)
            return [dict(row) for row in results]
        except Exception as e:
            logger.error(f"Failed to list users: {e}")
            return []
