"""Database adapter protocol and abstractions (SOLID - Interface Segregation Principle)."""

from typing import Any, Dict, List, Optional, Protocol, Sequence


class DatabaseAdapter(Protocol):
    """Minimal database adapter contract.

    Defines the interface that all adapters must implement.
    """

    connection: Any

    def connect(self) -> None:
        """Establish database connection."""
        ...

    def disconnect(self) -> None:
        """Close database connection."""
        ...

    def execute(self, sql: str, params: Sequence[Any] | None = None) -> Any:
        """Execute SQL query.

        Args:
            sql: SQL query string
            params: Query parameters

        Returns:
            Query result
        """
        ...

    def fetch_all(self, sql: str, params: Sequence[Any] | None = None) -> List[Dict]:
        """Fetch all rows from query.

        Args:
            sql: SQL query string
            params: Query parameters

        Returns:
            List of row dictionaries
        """
        ...

    def fetch_one(
        self, sql: str, params: Sequence[Any] | None = None
    ) -> Optional[Dict]:
        """Fetch single row from query.

        Args:
            sql: SQL query string
            params: Query parameters

        Returns:
            Single row dictionary or None
        """
        ...

    def get_tables(self) -> List[str]:
        """Get list of tables in database.

        Returns:
            List of table names
        """
        ...


class QueryCache(Protocol):
    """Query caching abstraction (for future implementation)."""

    def get(self, key: str) -> Optional[Any]:
        """Get cached query result.

        Args:
            key: Cache key

        Returns:
            Cached result or None
        """
        ...

    def put(self, key: str, value: Any, ttl: int | None = None) -> None:
        """Cache query result.

        Args:
            key: Cache key
            value: Result to cache
            ttl: Time-to-live in seconds
        """
        ...

    def invalidate(self, key: str) -> None:
        """Invalidate cache entry.

        Args:
            key: Cache key to invalidate
        """
        ...

    def clear(self) -> None:
        """Clear entire cache."""
        ...


class TransactionHandler(Protocol):
    """Transaction handling abstraction."""

    def begin(self) -> None:
        """Begin transaction."""
        ...

    def commit(self) -> None:
        """Commit transaction."""
        ...

    def rollback(self) -> None:
        """Rollback transaction."""
        ...

    @property
    def is_active(self) -> bool:
        """Check if transaction is active."""
        ...
