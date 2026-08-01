"""Enhanced tracing and observability utilities."""

from typing import Any, Callable, Optional

from core.db.logger import get_logger

logger = get_logger(__name__)


class TracingContext:
    """Context manager for distributed tracing."""

    def __init__(
        self, operation_name: str, attributes: Optional[dict[str, Any]] = None
    ):
        """Initialize tracing context.

        Args:
            operation_name: Name of the operation
            attributes: Additional attributes to track
        """
        self.operation_name = operation_name
        self.attributes = attributes or {}
        self.span = None

        try:
            from opentelemetry import trace

            self.tracer = trace.get_tracer(__name__)
        except Exception:
            self.tracer = None

    def __enter__(self):
        """Enter context (start span)."""
        if self.tracer:
            self.span = self.tracer.start_as_current_span(self.operation_name)
            if self.span:
                for key, value in self.attributes.items():
                    self.span.set_attribute(key, value)
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        """Exit context (end span)."""
        if self.span:
            if exc_type:
                self.span.set_attribute("error", True)
                self.span.set_attribute("error.type", exc_type.__name__)
            self.span.end()

    def add_attribute(self, key: str, value: Any) -> None:
        """Add attribute to span.

        Args:
            key: Attribute key
            value: Attribute value
        """
        if self.span:
            self.span.set_attribute(key, value)


def traced_operation(operation_name: str):
    """Decorator for tracing operations.

    Args:
        operation_name: Name of the operation

    Returns:
        Decorator function

    Example:
        @traced_operation("database.query")
        def fetch_users():
            return db.fetch_all("SELECT * FROM users")
    """

    def decorator(func: Callable) -> Callable:
        def wrapper(*args, **kwargs) -> Any:
            with TracingContext(operation_name, {"function": func.__name__}) as ctx:
                try:
                    result = func(*args, **kwargs)
                    ctx.add_attribute("success", True)
                    return result
                except Exception as e:
                    ctx.add_attribute("error", True)
                    ctx.add_attribute("error.message", str(e))
                    raise

        return wrapper

    return decorator


class PerformanceMonitor:
    """Monitor and log performance metrics."""

    def __init__(self, name: str, threshold_ms: float = 100.0):
        """Initialize performance monitor.

        Args:
            name: Monitor name
            threshold_ms: Threshold for logging slow operations
        """
        self.name = name
        self.threshold_ms = threshold_ms
        self.operations = []

    def __enter__(self):
        """Enter context (start monitoring)."""
        import time

        self._start = time.time()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        """Exit context (stop monitoring)."""
        import time

        elapsed_ms = (time.time() - self._start) * 1000

        operation_info = {
            "name": self.name,
            "elapsed_ms": elapsed_ms,
            "slow": elapsed_ms > self.threshold_ms,
        }
        self.operations.append(operation_info)

        if elapsed_ms > self.threshold_ms:
            logger.warning(
                f"Slow operation: {self.name} took {elapsed_ms:.2f}ms "
                f"(threshold: {self.threshold_ms}ms)"
            )
        else:
            logger.debug(f"Operation: {self.name} took {elapsed_ms:.2f}ms")

        if exc_type:
            operation_info["error"] = f"{exc_type.__name__}: {exc_val}"


def monitor_performance(name: str, threshold_ms: float = 100.0):
    """Decorator for performance monitoring.

    Args:
        name: Operation name
        threshold_ms: Threshold for logging slow operations

    Returns:
        Decorator function

    Example:
        @monitor_performance("db.query", threshold_ms=50)
        def fetch_users():
            return db.fetch_all("SELECT * FROM users")
    """

    def decorator(func: Callable) -> Callable:
        def wrapper(*args, **kwargs) -> Any:
            with PerformanceMonitor(name or func.__name__, threshold_ms):
                return func(*args, **kwargs)

        return wrapper

    return decorator
