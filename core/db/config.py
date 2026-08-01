"""Database configuration module following SOLID principles."""

from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from core.db.settings import DatabaseSettings


class DatabaseType(str, Enum):
    """Supported database types."""

    SQLITE = "sqlite"
    DUCKDB = "duckdb"


@dataclass
class DatabaseConfig:
    """Database configuration with performance settings.

    Attributes:
        db_type: Type of database
        connection_string: Connection string
        pool_size: Connection pool size
        max_overflow: Maximum overflow connections
        echo: Enable SQL echo
        timeout: Connection timeout in seconds
        extra_options: Additional database options
        settings: Performance and caching settings
    """

    db_type: DatabaseType
    connection_string: str
    pool_size: int = 5
    max_overflow: int = 10
    echo: bool = False
    timeout: int = 30
    extra_options: dict[str, Any] = field(default_factory=dict)
    settings: DatabaseSettings = field(default_factory=DatabaseSettings)

    @classmethod
    def from_sqlite(
        cls,
        path: str,
        pool_size: int | None = None,
        max_overflow: int = 10,
        echo: bool = False,
        settings: DatabaseSettings | None = None,
        **kwargs: Any,
    ) -> "DatabaseConfig":
        """Create SQLite configuration.

        Args:
            path: Path to SQLite database file
            pool_size: Connection pool size (ignored if settings.pool provided)
            max_overflow: Maximum overflow connections
            echo: Enable SQL echo
            settings: DatabaseSettings with pool and cache configuration
            **kwargs: Additional options

        Returns:
            DatabaseConfig instance
        """
        db_settings = settings or DatabaseSettings()
        # Use settings.pool.max_size if available, else fallback to pool_size
        effective_pool_size = (
            (db_settings.pool.max_size if db_settings.pool else None) or pool_size or 5
        )

        return cls(
            db_type=DatabaseType.SQLITE,
            connection_string=f"sqlite:///{path}",
            pool_size=effective_pool_size,
            max_overflow=max_overflow,
            echo=echo,
            settings=db_settings,
            extra_options=kwargs,
        )

    @classmethod
    def from_duckdb(
        cls,
        path: str,
        pool_size: int | None = None,
        max_overflow: int = 10,
        echo: bool = False,
        settings: DatabaseSettings | None = None,
        **kwargs: Any,
    ) -> "DatabaseConfig":
        """Create DuckDB configuration.

        Args:
            path: Path to DuckDB database file or :memory: for in-memory
            pool_size: Connection pool size (ignored if settings.pool provided)
            max_overflow: Maximum overflow connections
            echo: Enable SQL echo
            settings: DatabaseSettings with pool and cache configuration
            **kwargs: Additional options

        Returns:
            DatabaseConfig instance
        """
        db_settings = settings or DatabaseSettings()
        # Use settings.pool.max_size if available, else fallback to pool_size
        effective_pool_size = (
            (db_settings.pool.max_size if db_settings.pool else None) or pool_size or 5
        )

        return cls(
            db_type=DatabaseType.DUCKDB,
            connection_string=f"duckdb:///{path}",
            pool_size=effective_pool_size,
            max_overflow=max_overflow,
            echo=echo,
            settings=db_settings,
            extra_options=kwargs,
        )
