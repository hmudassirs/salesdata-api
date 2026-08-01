# core/app/settings.py
"""Application settings and configuration."""

import logging
from dataclasses import dataclass
from typing import Optional

logger = logging.getLogger(__name__)


@dataclass
class AppSettings:
    """Application configuration settings."""

    app_name: str = "preparedata"
    debug: bool = False
    log_level: str = "INFO"
    database_url: Optional[str] = None
    service_db_path: str = "data/service.db"
    pool_size: int = 5
    max_overflow: int = 10

    # --- Auth / JWT ---
    # No hardcoded default: fail loudly rather than silently signing
    # tokens with a well-known secret.
    jwt_secret_key: Optional[str] = None
    jwt_algorithm: str = "HS256"
    jwt_expiry_seconds: int = 3600

    # --- CORS ---
    # Comma-separated list of allowed origins. "*" is only acceptable
    # when allow_credentials is False; validated in from_env().
    cors_allow_origins: tuple = ("*",)
    cors_allow_credentials: bool = False

    @classmethod
    def from_env(cls) -> "AppSettings":
        """Create settings from environment variables.

        Returns:
            AppSettings instance

        Raises:
            RuntimeError: If JWT_SECRET_KEY is not set, or if CORS is
                configured with both a wildcard origin and credentials
                enabled (an insecure, browser-rejected combination).
        """
        import os

        jwt_secret_key = os.getenv("JWT_SECRET_KEY")
        if not jwt_secret_key:
            raise RuntimeError(
                "JWT_SECRET_KEY environment variable must be set. "
                "Refusing to start with no signing secret or a hardcoded default."
            )
        if len(jwt_secret_key.encode()) < 32:
            logger.warning(
                "JWT_SECRET_KEY is only %d bytes; RFC 7518 recommends at least 32 "
                "for HS256. Generate a proper one, e.g.: "
                'python -c "import secrets; print(secrets.token_hex(32))"',
                len(jwt_secret_key.encode()),
            )

        cors_origins_raw = os.getenv("CORS_ALLOW_ORIGINS", "*")
        cors_allow_origins = tuple(
            o.strip() for o in cors_origins_raw.split(",") if o.strip()
        )
        cors_allow_credentials = (
            os.getenv("CORS_ALLOW_CREDENTIALS", "false").lower() == "true"
        )

        if cors_allow_credentials and "*" in cors_allow_origins:
            raise RuntimeError(
                "CORS_ALLOW_ORIGINS cannot be '*' when CORS_ALLOW_CREDENTIALS is true. "
                "List explicit origins instead."
            )

        return cls(
            app_name=os.getenv("APP_NAME", "preparedata"),
            debug=os.getenv("DEBUG", "false").lower() == "true",
            log_level=os.getenv("LOG_LEVEL", "INFO"),
            database_url=os.getenv("DATABASE_URL"),
            pool_size=int(os.getenv("POOL_SIZE", "5")),
            max_overflow=int(os.getenv("MAX_OVERFLOW", "10")),
            service_db_path=os.getenv("SERVICE_DB_PATH", "data/service.db"),
            jwt_secret_key=jwt_secret_key,
            jwt_algorithm=os.getenv("JWT_ALGORITHM", "HS256"),
            jwt_expiry_seconds=int(os.getenv("JWT_EXPIRY_SECONDS", "3600")),
            cors_allow_origins=cors_allow_origins,
            cors_allow_credentials=cors_allow_credentials,
        )

    def configure_logging(self) -> None:
        """Configure logging based on settings."""
        logging.basicConfig(
            level=getattr(logging, self.log_level),
            format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
        )
        logger.info(f"Logging configured with level: {self.log_level}")
