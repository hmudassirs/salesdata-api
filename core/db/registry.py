"""Plugin architecture for database adapters and extensions."""

from typing import Any, Dict, Optional, Type

from core.db.logger import get_logger
from core.db.protocols import DatabaseAdapter

logger = get_logger(__name__)


class AdapterRegistry:
    """Registry for database adapters supporting plugin architecture."""

    def __init__(self):
        """Initialize adapter registry."""
        self._adapters: Dict[str, Type[DatabaseAdapter]] = {}
        self._instances: Dict[str, DatabaseAdapter] = {}

    def register(self, name: str, adapter_class: Type[DatabaseAdapter]) -> None:
        """Register a database adapter.

        Args:
            name: Adapter name (e.g., 'duckdb', 'sqlite', 'postgres')
            adapter_class: Adapter class (must implement DatabaseAdapter protocol)

        Raises:
            ValueError: If adapter name already registered
        """
        if name in self._adapters:
            raise ValueError(f"Adapter '{name}' already registered")

        self._adapters[name] = adapter_class
        logger.info(f"Registered adapter: {name}")

    def unregister(self, name: str) -> None:
        """Unregister a database adapter.

        Args:
            name: Adapter name

        Raises:
            ValueError: If adapter not found
        """
        if name not in self._adapters:
            raise ValueError(f"Adapter '{name}' not found")

        del self._adapters[name]
        if name in self._instances:
            del self._instances[name]
        logger.info(f"Unregistered adapter: {name}")

    def get_adapter_class(self, name: str) -> Type[DatabaseAdapter]:
        """Get adapter class by name.

        Args:
            name: Adapter name

        Returns:
            Adapter class

        Raises:
            ValueError: If adapter not found
        """
        if name not in self._adapters:
            raise ValueError(
                f"Adapter '{name}' not found. Available: {list(self._adapters.keys())}"
            )
        return self._adapters[name]

    def create_adapter(self, name: str, *args: Any, **kwargs: Any) -> DatabaseAdapter:
        """Create adapter instance.

        Args:
            name: Adapter name
            *args: Positional arguments for adapter constructor
            **kwargs: Keyword arguments for adapter constructor

        Returns:
            Adapter instance

        Raises:
            ValueError: If adapter not found
        """
        adapter_class = self.get_adapter_class(name)
        adapter = adapter_class(*args, **kwargs)
        logger.debug(f"Created adapter instance: {name}")
        return adapter

    def list_adapters(self) -> list[str]:
        """Get list of registered adapter names.

        Returns:
            List of adapter names
        """
        return list(self._adapters.keys())

    def is_registered(self, name: str) -> bool:
        """Check if adapter is registered.

        Args:
            name: Adapter name

        Returns:
            True if registered, False otherwise
        """
        return name in self._adapters

    def clear(self) -> None:
        """Clear all registered adapters."""
        self._adapters.clear()
        self._instances.clear()
        logger.info("Adapter registry cleared")


# Global registry instance
_adapter_registry: Optional[AdapterRegistry] = None


def get_adapter_registry() -> AdapterRegistry:
    """Get or create global adapter registry.

    Returns:
        AdapterRegistry instance
    """
    global _adapter_registry
    if _adapter_registry is None:
        _adapter_registry = AdapterRegistry()
    return _adapter_registry


def register_builtin_adapters() -> None:
    """Register built-in database adapters."""
    from core.db.adapters.duckdb import DuckDBAdapter
    from core.db.adapters.sqlite import SQLiteAdapter

    registry = get_adapter_registry()

    if not registry.is_registered("duckdb"):
        registry.register("duckdb", DuckDBAdapter)

    if not registry.is_registered("sqlite"):
        registry.register("sqlite", SQLiteAdapter)

    logger.info("Built-in adapters registered")
