"""Cache and performance settings for database operations."""

from dataclasses import dataclass
from enum import Enum


class CacheStrategy(str, Enum):
    """Supported caching strategies."""

    NONE = "none"
    LRU = "lru"  # Least Recently Used
    TTL = "ttl"  # Time To Live
    HYBRID = "hybrid"  # Combines LRU and TTL


@dataclass
class CacheSettings:
    """Configuration for database query caching.

    Attributes:
        enabled: Whether caching is enabled
        strategy: Caching strategy to use
        max_size: Maximum number of cached items
        ttl_seconds: Time-to-live in seconds (for TTL strategy)
        eviction_policy: What to do when cache is full
    """

    enabled: bool = False
    strategy: CacheStrategy = CacheStrategy.NONE
    max_size: int = 256
    ttl_seconds: int = 3600  # 1 hour default
    eviction_policy: str = "lru"

    def validate(self) -> None:
        """Validate cache settings.

        Raises:
            ValueError: If settings are invalid
        """
        if self.max_size <= 0:
            raise ValueError("max_size must be positive")
        if self.ttl_seconds <= 0:
            raise ValueError("ttl_seconds must be positive")
        if self.strategy == CacheStrategy.NONE and self.enabled:
            raise ValueError("Cannot enable cache with NONE strategy")


@dataclass
class PoolSettings:
    """Connection pool performance settings.

    Attributes:
        min_size: Minimum number of connections to maintain
        max_size: Maximum number of connections
        max_overflow: How many connections above max_size are allowed
        timeout: Connection acquisition timeout in seconds
        recycle_interval: Recycle connections after N seconds (None = no recycle)
        echo: Log all SQL statements
    """

    min_size: int = 1
    max_size: int = 20
    max_overflow: int = 10
    timeout: int = 30
    recycle_interval: int | None = None
    echo: bool = False

    def validate(self) -> None:
        """Validate pool settings.

        Raises:
            ValueError: If settings are invalid
        """
        if self.min_size <= 0:
            raise ValueError("min_size must be positive")
        if self.max_size <= 0:
            raise ValueError("max_size must be positive")
        if self.min_size > self.max_size:
            raise ValueError("min_size cannot exceed max_size")
        if self.max_overflow < 0:
            raise ValueError("max_overflow cannot be negative")
        if self.timeout <= 0:
            raise ValueError("timeout must be positive")
        if self.recycle_interval is not None and self.recycle_interval <= 0:
            raise ValueError("recycle_interval must be positive if set")


@dataclass
class DatabaseSettings:
    """Combined database performance and behavior settings.

    Attributes:
        cache: Cache settings
        pool: Pool settings
        enable_metrics: Enable performance metrics collection
        enable_tracing: Enable distributed tracing
    """

    cache: CacheSettings | None = None
    pool: PoolSettings | None = None
    enable_metrics: bool = True
    enable_tracing: bool = False

    def __post_init__(self) -> None:
        """Initialize defaults and validate settings."""
        if self.cache is None:
            self.cache = CacheSettings()
        if self.pool is None:
            self.pool = PoolSettings()

    def validate(self) -> None:
        """Validate all settings.

        Raises:
            ValueError: If any settings are invalid
        """
        self.cache.validate()
        self.pool.validate()
