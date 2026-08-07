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

    # --- Query execution policy (roadmap Phase 13.1) ---
    # This deployment is an intentional "authenticated DB console", not
    # a read-only reporting API: callers are expected to run DDL/DML
    # against their own warehouse, and there's no per-row/per-tenant
    # data to leak between callers (single shared warehouse, no RLS
    # concept anywhere in this codebase -- see QueryCacheCoordinator's
    # docstring for the corresponding cache-isolation decision). Given
    # that, the policy chosen here is: SELECT/WITH require only the
    # "read" scope (or any authenticated caller, since read is always
    # granted); write statements (INSERT/UPDATE/DELETE/DDL/etc.)
    # require the "write" scope on the caller's API key, or an "admin"
    # role for JWT-authenticated sessions (which have no scope concept
    # of their own). See core/db/sql_policy.py.
    require_write_scope_for_mutations: bool = True

    # --- Query result limits (roadmap Phase 13.4) ---
    # Protects one caller's unbounded SELECT from exhausting memory or
    # the response pipeline. Rows beyond this are dropped and the
    # response is flagged `truncated=True` rather than silently
    # returning a partial result with no indication anything was cut.
    max_result_rows: int = 10_000
    # Same idea, but on serialized response size -- catches wide rows
    # (many/large columns) that a row-count cap alone wouldn't.
    max_result_bytes: int = 10 * 1024 * 1024  # 10 MiB
    # A query running longer than this is cancelled and reported as an
    # error rather than left to run indefinitely and hold a pool
    # connection open.
    max_query_duration_seconds: float = 30.0

    # --- Concurrency controls for expensive queries (roadmap Phase 14) ---
    # Separate semaphores per query class so a handful of expensive
    # aggregations/wide scans can't starve simple/fast queries of pool
    # capacity. See core/db/sql_policy.py for classification and
    # core/services/query_service.py for where these are applied.
    # Intentionally >= pool max_size: these gate *how many callers can
    # be mid-execution*, not how many DB connections exist -- the pool
    # itself is still the hard ceiling on actual DB concurrency.
    #
    # None (the default) means "derive from the host's actual CPU count
    # via core.concurrency.cpu.recommended_sizing()" instead of a
    # constant picked for one machine -- resolved in from_env(). Pass an
    # explicit int (or set the matching env var) to pin a fixed value
    # instead, e.g. for a deployment that's benchmarked its own optimum.
    fast_query_concurrency_limit: Optional[int] = None
    normal_query_concurrency_limit: Optional[int] = None
    expensive_query_concurrency_limit: Optional[int] = None

    # --- Cache invalidation ---
    # When True, a write statement invalidates only the cache entries
    # for tables it plausibly touched (core.db.sql_policy.extract_tables),
    # instead of clearing the entire query cache. Falls back to a full
    # clear automatically whenever no table can be resolved from the
    # statement. Flagged so a full clear-on-every-write can be restored
    # (the previous, maximally-conservative behavior) if the heuristic
    # ever misses a table reference in production.
    cache_invalidation_precise: bool = True

    # --- Auth rate limiting ---
    # Basic in-process, per-IP sliding-window limiter on
    # /api/auth/users/login and /api/auth/users/register (see
    # core.auth.rate_limiter) -- both are unauthenticated endpoints, so
    # they'd otherwise have no protection against credential stuffing
    # or registration spam. Flagged off for deployments that already
    # rate-limit at a reverse proxy/gateway layer. Per-process, like the
    # other in-memory caches in this codebase: not shared across
    # multiple workers/instances.
    auth_rate_limit_enabled: bool = True
    auth_rate_limit_max_attempts: int = 10
    auth_rate_limit_window_seconds: float = 60.0

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

        # Resolve query-concurrency defaults from the host's actual CPU
        # count (core/concurrency/cpu.py) unless an env var pins an
        # explicit value -- see the field docstrings above.
        from core.concurrency.cpu import recommended_sizing

        sizing = recommended_sizing()
        fast_limit = os.getenv("FAST_QUERY_CONCURRENCY_LIMIT")
        normal_limit = os.getenv("NORMAL_QUERY_CONCURRENCY_LIMIT")
        expensive_limit = os.getenv("EXPENSIVE_QUERY_CONCURRENCY_LIMIT")

        return cls(
            app_name=os.getenv("APP_NAME", "salesinfo-api"),
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
            require_write_scope_for_mutations=(
                os.getenv("REQUIRE_WRITE_SCOPE_FOR_MUTATIONS", "true").lower()
                == "true"
            ),
            max_result_rows=int(os.getenv("MAX_RESULT_ROWS", "10000")),
            max_result_bytes=int(os.getenv("MAX_RESULT_BYTES", str(10 * 1024 * 1024))),
            max_query_duration_seconds=float(
                os.getenv("MAX_QUERY_DURATION_SECONDS", "30.0")
            ),
            fast_query_concurrency_limit=int(
                fast_limit if fast_limit is not None else sizing.fast_query_concurrency
            ),
            normal_query_concurrency_limit=int(
                normal_limit
                if normal_limit is not None
                else sizing.normal_query_concurrency
            ),
            expensive_query_concurrency_limit=int(
                expensive_limit
                if expensive_limit is not None
                else sizing.expensive_query_concurrency
            ),
            cache_invalidation_precise=(
                os.getenv("CACHE_INVALIDATION_PRECISE", "true").lower() == "true"
            ),
            auth_rate_limit_enabled=(
                os.getenv("AUTH_RATE_LIMIT_ENABLED", "true").lower() == "true"
            ),
            auth_rate_limit_max_attempts=int(
                os.getenv("AUTH_RATE_LIMIT_MAX_ATTEMPTS", "10")
            ),
            auth_rate_limit_window_seconds=float(
                os.getenv("AUTH_RATE_LIMIT_WINDOW_SECONDS", "60.0")
            ),
        )

    def configure_logging(self) -> None:
        """Configure logging based on settings."""
        logging.basicConfig(
            level=getattr(logging, self.log_level),
            format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
        )
        logger.info(f"Logging configured with level: {self.log_level}")
