"""Centralized logging configuration for the database package."""

import logging
from typing import Optional


class LoggerManager:
    """Centralized logger management following Dependency Inversion Principle."""

    _loggers: dict[str, logging.Logger] = {}
    _default_level: int = logging.INFO
    _format_string: str = "%(asctime)s - %(name)s - %(levelname)s - %(message)s"

    @classmethod
    def configure(
        cls,
        level: int = logging.INFO,
        format_string: Optional[str] = None,
    ) -> None:
        """Configure global logging settings.

        Args:
            level: Logging level (e.g., logging.INFO, logging.DEBUG)
            format_string: Log format string (uses default if None)
        """
        cls._default_level = level
        if format_string:
            cls._format_string = format_string

        # Reconfigure existing loggers
        for logger in cls._loggers.values():
            logger.setLevel(level)
            for handler in logger.handlers:
                if isinstance(handler, logging.StreamHandler):
                    formatter = logging.Formatter(cls._format_string)
                    handler.setFormatter(formatter)

    @classmethod
    def get_logger(cls, name: str) -> logging.Logger:
        """Get or create logger instance.

        Args:
            name: Logger name (typically __name__)

        Returns:
            Configured logger instance
        """
        if name not in cls._loggers:
            logger = logging.getLogger(name)
            logger.setLevel(cls._default_level)

            # Avoid duplicate handlers
            if not logger.handlers:
                handler = logging.StreamHandler()
                formatter = logging.Formatter(cls._format_string)
                handler.setFormatter(formatter)
                logger.addHandler(handler)

            cls._loggers[name] = logger

        return cls._loggers[name]

    @classmethod
    def reset(cls) -> None:
        """Reset all loggers to default state."""
        cls._loggers.clear()
        cls._default_level = logging.INFO


# Convenience function for module-level imports
def get_logger(name: str) -> logging.Logger:
    """Get logger instance.

    Args:
        name: Logger name

    Returns:
        Configured logger instance
    """
    return LoggerManager.get_logger(name)
