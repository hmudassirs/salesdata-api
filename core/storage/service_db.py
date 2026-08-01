"""Service database initialization for API keys, logging, and caching.

This creates a separate SQLite database for service-related data:
- API keys and users
- Logging and traces
- Query caching

The main data warehouse remains in DuckDB.
"""

import os
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional

from core.auth.passwords import hash_password
from core.db.adapters.sqlite import SQLiteAdapter
from core.db.logger import get_logger
from core.db.pool import MaxConnectionsExceeded, SyncConnectionPool

# Same optional Prometheus instrumentation session.py applies around the
# DuckDB warehouse pool's acquire/release. Optional because — same as
# session.py — this must not become a hard dependency just to run a
# query; if prometheus_client isn't installed, these stay None and the
# instrumentation below is skipped.
try:
    from core.observability import POOL_ACTIVE, POOL_REQUESTS, POOL_TIMEOUTS, POOL_WAIT
except Exception:
    POOL_REQUESTS = POOL_WAIT = POOL_ACTIVE = POOL_TIMEOUTS = None

logger = get_logger(__name__)


@dataclass
class ExecuteResult:
    """What execute() returns instead of a live sqlite3.Cursor.

    A pooled connection is released the instant execute() returns, so a
    cursor handed back to the caller could end up being read from a
    connection another thread has since borrowed and reused. lastrowid/
    rowcount are plain values captured before release, so they stay
    valid to read afterward; fetching more rows off the cursor is not.
    """

    lastrowid: Optional[int]
    rowcount: int


class ServiceDatabase:
    """Service database manager for API keys, logging, and caching.

    Composes the same SQLiteAdapter + SyncConnectionPool used elsewhere
    in core.db, rather than hand-rolling connection pooling here. Each
    pooled connection is a fully independent SQLiteAdapter (its own
    sqlite3.Connection) — sharing one connection across concurrent
    callers was the root cause of the "bad parameter or other API
    misuse" corruption this class used to produce under load.
    """

    def __init__(
        self,
        db_path: str = "data/service.db",
        min_size: int = 2,
        max_size: int = 8,
        timeout: float = 30.0,
    ):
        """Initialize service database.

        Args:
            db_path: Path to SQLite database file
            min_size: Connections the pool may keep idle
            max_size: Ceiling on connections ever opened against this file
            timeout: Seconds to wait for a free pooled connection, and
                passed to sqlite3 as its busy_timeout so a brief write
                lock is retried instead of raising immediately
        """
        self.db_path = db_path
        self.min_size = min_size
        self.max_size = max_size
        self.timeout = timeout
        self._pool: Optional[SyncConnectionPool] = None

    def _create_connection(self) -> SQLiteAdapter:
        """Factory passed to SyncConnectionPool. Must open a genuinely
        independent connection on every call — see the class docstring."""
        adapter = SQLiteAdapter(self.db_path)
        adapter.connect(timeout=int(self.timeout), check_same_thread=False)
        # journal_mode is a database-level setting that persists in the
        # file after the first connection sets it — cheap/safe to
        # re-assert here. synchronous=NORMAL is the recommended pairing
        # with WAL: still durable across an app crash, just not fsync'd
        # on every single commit like the default FULL mode.
        adapter.connection.execute("PRAGMA journal_mode=WAL")
        adapter.connection.execute("PRAGMA synchronous=NORMAL")
        return adapter

    def connect(self) -> None:
        """Open the connection pool against the service database file."""
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        self._pool = SyncConnectionPool(
            create_connection=self._create_connection,
            min_conn=self.min_size,
            max_conn=self.max_size,
            timeout=self.timeout,
        )
        logger.info(
            f"Connected to service database: {self.db_path} "
            f"(pool min={self.min_size}, max={self.max_size})"
        )

    def disconnect(self) -> None:
        """Close every pooled connection."""
        if self._pool:
            self._pool.close_all()
            logger.info("Disconnected from service database")

    def metrics(self) -> Dict[str, Any]:
        """Pool metrics, for the /api/health endpoint."""
        if not self._pool:
            raise RuntimeError("Not connected to service database")
        return dict(self._pool.metrics())

    @contextmanager
    def _acquire(self):
        """Acquire a pooled connection with the same Prometheus
        instrumentation session.py applies around the DuckDB warehouse
        pool's acquire/release — this pool was otherwise invisible to
        the same metrics. All failures here are non-fatal to the
        instrumentation itself; a broken metrics backend must never
        break a query."""
        if not self._pool:
            raise RuntimeError("Not connected to service database")

        if POOL_REQUESTS is not None:
            try:
                POOL_REQUESTS.inc()
            except Exception:
                logger.debug("Failed to increment POOL_REQUESTS", exc_info=True)

        start = time.monotonic()
        try:
            adapter = self._pool.acquire()
        except MaxConnectionsExceeded:
            if POOL_TIMEOUTS is not None:
                try:
                    POOL_TIMEOUTS.inc()
                except Exception:
                    logger.debug("Failed to increment POOL_TIMEOUTS", exc_info=True)
            raise
        finally:
            if POOL_WAIT is not None:
                try:
                    POOL_WAIT.observe(time.monotonic() - start)
                except Exception:
                    logger.debug("Failed to observe POOL_WAIT", exc_info=True)

        if POOL_ACTIVE is not None:
            try:
                POOL_ACTIVE.inc()
            except Exception:
                logger.debug("Failed to increment POOL_ACTIVE", exc_info=True)

        try:
            yield adapter
        finally:
            self._pool.release(adapter)
            if POOL_ACTIVE is not None:
                try:
                    POOL_ACTIVE.dec()
                except Exception:
                    logger.debug("Failed to decrement POOL_ACTIVE", exc_info=True)

    def execute_on(self, adapter, sql: str, params: tuple = ()) -> ExecuteResult:
        """Run a statement on an already-acquired adapter, without
        acquiring a new connection or committing — for use inside
        `transaction()`, where the caller controls the commit boundary.
        """
        cursor = adapter.execute(sql, params)
        return ExecuteResult(lastrowid=cursor.lastrowid, rowcount=cursor.rowcount)

    def fetch_one_on(self, adapter, sql: str, params: tuple = ()):
        """Like fetch_one, but against an already-acquired adapter — see
        execute_on()."""
        cursor = adapter.execute(sql, params)
        return cursor.fetchone()

    @contextmanager
    def transaction(self):
        """Acquire one connection for multiple statements, committing
        once at the end instead of once per statement.

        Built for emit_request_observability(), which does ~4 writes
        (log_request, start_trace, end_trace, audit) per request. Each
        used to go through its own execute() call — its own acquire,
        its own commit, its own release — meaning 4 separate trips
        through SQLite's single-writer lock per request. Batching them
        into one transaction takes that lock once per request instead
        of four times. Total write volume is unchanged; what changes is
        how many times concurrent requests contend for the lock.

        Usage:
            with service_db.transaction() as adapter:
                service_db.execute_on(adapter, sql1, params1)
                service_db.execute_on(adapter, sql2, params2)
            # commits here on success, rolls back on exception
        """
        with self._acquire() as adapter:
            try:
                yield adapter
                adapter.connection.commit()
            except Exception:
                try:
                    adapter.connection.rollback()
                except Exception:
                    logger.debug("Rollback failed", exc_info=True)
                raise

    def execute(self, sql: str, params: tuple = ()) -> ExecuteResult:
        """Execute a write statement (INSERT/UPDATE/DELETE/DDL) and commit.

        Not meant for SELECTs — see ExecuteResult's docstring for why a
        live cursor isn't handed back. Use fetch_one/fetch_all for reads.

        Args:
            sql: SQL statement string
            params: Statement parameters

        Returns:
            ExecuteResult(lastrowid, rowcount)
        """
        with self._acquire() as adapter:
            try:
                cursor = adapter.execute(sql, params)
                if not sql.strip().upper().startswith("SELECT"):
                    adapter.connection.commit()
                return ExecuteResult(
                    lastrowid=cursor.lastrowid, rowcount=cursor.rowcount
                )
            except Exception as e:
                logger.error(f"Service database query failed: {e}")
                raise

    def fetch_one(self, sql: str, params: tuple = ()):
        """Fetch a single row.

        Returns a sqlite3.Row (not a plain dict): several call sites in
        service_manager.py index results positionally (e.g. result[0]),
        which a dict doesn't support — sqlite3.Row supports both
        row["col"] and row[0].

        Args:
            sql: SQL query string
            params: Query parameters

        Returns:
            Single row or None
        """
        with self._acquire() as adapter:
            try:
                cursor = adapter.execute(sql, params)
                return cursor.fetchone()
            except Exception as e:
                logger.error(f"Service database query failed: {e}")
                raise

    def fetch_all(self, sql: str, params: tuple = ()):
        """Fetch all rows. See fetch_one() for the sqlite3.Row note.

        Args:
            sql: SQL query string
            params: Query parameters

        Returns:
            List of rows
        """
        with self._acquire() as adapter:
            try:
                cursor = adapter.execute(sql, params)
                return cursor.fetchall()
            except Exception as e:
                logger.error(f"Service database query failed: {e}")
                raise

    def create_tables(self) -> None:
        """Create all service database tables."""
        self._create_api_keys_table()
        self._create_users_table()
        self._create_logs_table()
        self._create_traces_table()
        self._create_query_cache_table()
        self._create_audit_log_table()

        logger.info("Service database tables created")

    def _create_api_keys_table(self) -> None:
        """Create API keys table."""
        # Create table
        self.execute("""
        CREATE TABLE IF NOT EXISTS api_keys (
            key_id VARCHAR PRIMARY KEY,
            api_key_hash VARCHAR NOT NULL,
            owner_id VARCHAR NOT NULL,
            created_at INTEGER NOT NULL,
            expires_at INTEGER,
            scopes VARCHAR,
            is_active BOOLEAN DEFAULT 1,
            last_used_at INTEGER,
            usage_count INTEGER DEFAULT 0,
            FOREIGN KEY (owner_id) REFERENCES users(user_id)
        )
        """)

        # Create indexes
        self.execute(
            "CREATE INDEX IF NOT EXISTS idx_api_keys_owner ON api_keys(owner_id)"
        )
        self.execute(
            "CREATE INDEX IF NOT EXISTS idx_api_keys_hash ON api_keys(api_key_hash)"
        )
        self.execute(
            "CREATE INDEX IF NOT EXISTS idx_api_keys_active ON api_keys(is_active)"
        )

    def _create_users_table(self) -> None:
        """Create users table."""
        # Create table
        self.execute("""
        CREATE TABLE IF NOT EXISTS users (
            user_id VARCHAR PRIMARY KEY,
            username VARCHAR NOT NULL UNIQUE,
            email VARCHAR NOT NULL UNIQUE,
            password_hash VARCHAR NOT NULL,
            roles VARCHAR DEFAULT 'viewer',
            is_active BOOLEAN DEFAULT 1,
            created_at INTEGER NOT NULL,
            updated_at INTEGER NOT NULL,
            last_login_at INTEGER,
            login_count INTEGER DEFAULT 0
        )
        """)

        # Create indexes
        self.execute("CREATE INDEX IF NOT EXISTS idx_users_username ON users(username)")
        self.execute("CREATE INDEX IF NOT EXISTS idx_users_email ON users(email)")
        self.execute("CREATE INDEX IF NOT EXISTS idx_users_active ON users(is_active)")

    def _create_logs_table(self) -> None:
        """Create application logs table."""
        # Create table
        self.execute("""
        CREATE TABLE IF NOT EXISTS logs (
            log_id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp INTEGER NOT NULL,
            level VARCHAR NOT NULL,
            logger VARCHAR NOT NULL,
            message TEXT NOT NULL,
            module VARCHAR,
            function VARCHAR,
            line INTEGER,
            exception TEXT,
            user_id VARCHAR,
            session_id VARCHAR,
            request_id VARCHAR,
            ip_address VARCHAR,
            user_agent TEXT,
            FOREIGN KEY (user_id) REFERENCES users(user_id)
        )
        """)

        # Create indexes
        self.execute("CREATE INDEX IF NOT EXISTS idx_logs_timestamp ON logs(timestamp)")
        self.execute("CREATE INDEX IF NOT EXISTS idx_logs_level ON logs(level)")
        self.execute("CREATE INDEX IF NOT EXISTS idx_logs_logger ON logs(logger)")
        self.execute("CREATE INDEX IF NOT EXISTS idx_logs_user ON logs(user_id)")
        self.execute("CREATE INDEX IF NOT EXISTS idx_logs_request ON logs(request_id)")

    def _create_traces_table(self) -> None:
        """Create distributed tracing table."""
        # Create table
        self.execute("""
        CREATE TABLE IF NOT EXISTS traces (
            trace_id VARCHAR PRIMARY KEY,
            span_id VARCHAR NOT NULL,
            parent_span_id VARCHAR,
            operation_name VARCHAR NOT NULL,
            start_time INTEGER NOT NULL,
            end_time INTEGER,
            duration_ms INTEGER,
            status VARCHAR,
            error_message TEXT,
            service_name VARCHAR NOT NULL,
            service_version VARCHAR,
            user_id VARCHAR,
            session_id VARCHAR,
            request_id VARCHAR,
            http_method VARCHAR,
            http_url TEXT,
            http_status_code INTEGER,
            db_query TEXT,
            db_duration_ms INTEGER,
            tags TEXT,  -- JSON string
            FOREIGN KEY (user_id) REFERENCES users(user_id)
        )
        """)

        # Create indexes
        self.execute(
            "CREATE INDEX IF NOT EXISTS idx_traces_start_time ON traces(start_time)"
        )
        self.execute(
            "CREATE INDEX IF NOT EXISTS idx_traces_operation ON traces(operation_name)"
        )
        self.execute(
            "CREATE INDEX IF NOT EXISTS idx_traces_service ON traces(service_name)"
        )
        self.execute("CREATE INDEX IF NOT EXISTS idx_traces_user ON traces(user_id)")
        self.execute(
            "CREATE INDEX IF NOT EXISTS idx_traces_request ON traces(request_id)"
        )
        self.execute(
            "CREATE INDEX IF NOT EXISTS idx_traces_parent ON traces(parent_span_id)"
        )

    def _create_query_cache_table(self) -> None:
        """Create query result caching table."""
        # Create table
        self.execute("""
        CREATE TABLE IF NOT EXISTS query_cache (
            cache_key VARCHAR PRIMARY KEY,
            query_hash VARCHAR NOT NULL,
            query_sql TEXT NOT NULL,
            result_data TEXT NOT NULL,  -- JSON string
            result_count INTEGER DEFAULT 0,
            created_at INTEGER NOT NULL,
            expires_at INTEGER,
            last_accessed_at INTEGER,
            access_count INTEGER DEFAULT 0,
            user_id VARCHAR,
            session_id VARCHAR,
            execution_time_ms INTEGER,
            result_size_bytes INTEGER,
            is_compressed BOOLEAN DEFAULT 0,
            FOREIGN KEY (user_id) REFERENCES users(user_id)
        )
        """)

        # Create indexes
        self.execute(
            "CREATE INDEX IF NOT EXISTS idx_cache_query_hash ON query_cache(query_hash)"
        )
        self.execute(
            "CREATE INDEX IF NOT EXISTS idx_cache_expires ON query_cache(expires_at)"
        )
        self.execute(
            "CREATE INDEX IF NOT EXISTS idx_cache_accessed ON query_cache(last_accessed_at)"
        )
        self.execute(
            "CREATE INDEX IF NOT EXISTS idx_cache_user ON query_cache(user_id)"
        )

    def _create_audit_log_table(self) -> None:
        """Create audit log table for security events."""
        # Create table
        self.execute("""
        CREATE TABLE IF NOT EXISTS audit_log (
            audit_id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp INTEGER NOT NULL,
            event_type VARCHAR NOT NULL,
            user_id VARCHAR,
            session_id VARCHAR,
            ip_address VARCHAR,
            user_agent TEXT,
            resource_type VARCHAR,
            resource_id VARCHAR,
            action VARCHAR NOT NULL,
            old_values TEXT,  -- JSON string
            new_values TEXT,  -- JSON string
            success BOOLEAN DEFAULT 1,
            error_message TEXT,
            metadata TEXT,  -- JSON string
            FOREIGN KEY (user_id) REFERENCES users(user_id)
        )
        """)

        # Create indexes
        self.execute(
            "CREATE INDEX IF NOT EXISTS idx_audit_timestamp ON audit_log(timestamp)"
        )
        self.execute(
            "CREATE INDEX IF NOT EXISTS idx_audit_event ON audit_log(event_type)"
        )
        self.execute("CREATE INDEX IF NOT EXISTS idx_audit_user ON audit_log(user_id)")
        self.execute(
            "CREATE INDEX IF NOT EXISTS idx_audit_resource ON audit_log(resource_type, resource_id)"
        )

    def initialize_admin_user(self) -> None:
        """Create an initial admin user, but only from an explicitly
        configured password — never a hardcoded default.

        The previous version of this method created a well-known
        `admin` / `admin123!` account (hashed with unsalted SHA-256) on
        every fresh database with no forced rotation. Any deployment
        that didn't immediately notice and change it had a public,
        guessable admin login sitting in the database. This version is
        a no-op unless `INITIAL_ADMIN_PASSWORD` is set in the
        environment; the recommended way to create the first admin is
        the standalone `bootstrap_admin.py` script (interactive
        password prompt, no plaintext in shell history/process list).
        This env-var path exists mainly for scripted/CI first-boot
        setups where a prompt isn't possible.
        """
        password = os.getenv("INITIAL_ADMIN_PASSWORD")
        username = os.getenv("INITIAL_ADMIN_USERNAME", "admin")

        if not password:
            result = self.fetch_one(
                "SELECT COUNT(*) as count FROM users WHERE roles LIKE '%admin%'"
            )
            if result and result[0] == 0:
                logger.warning(
                    "No admin user exists yet. Run bootstrap_admin.py, or set "
                    "INITIAL_ADMIN_USERNAME/INITIAL_ADMIN_EMAIL/"
                    "INITIAL_ADMIN_PASSWORD before startup, to create one. "
                    "No default admin account will be created automatically."
                )
            return

        if len(password) < 8:
            logger.error(
                "INITIAL_ADMIN_PASSWORD is shorter than 8 characters; refusing "
                "to create an admin user with a weak password."
            )
            return

        existing = self.fetch_one(
            "SELECT COUNT(*) as count FROM users WHERE username = ?", (username,)
        )
        if existing and existing[0] > 0:
            logger.info(f"User '{username}' already exists; skipping admin bootstrap.")
            return

        email = os.getenv("INITIAL_ADMIN_EMAIL", f"{username}@preparedata.local")
        user_id = f"user_admin_{int(time.time())}"
        password_hash = hash_password(password)
        created_at = int(time.time())

        sql = """
        INSERT INTO users (user_id, username, email, password_hash, roles, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """
        self.execute(
            sql,
            (user_id, username, email, password_hash, "admin", created_at, created_at),
        )

        logger.info(f"Created initial admin user '{username}' (user_id={user_id})")

    def cleanup_expired_cache(self) -> int:
        """Clean up expired cache entries.

        Returns:
            Number of entries cleaned up
        """
        current_time = int(time.time())
        result = self.execute(
            "DELETE FROM query_cache WHERE expires_at IS NOT NULL AND expires_at < ?",
            (current_time,),
        )
        deleted_count = result.rowcount
        if deleted_count > 0:
            logger.info(f"Cleaned up {deleted_count} expired cache entries")
        return deleted_count

    def get_stats(self) -> dict:
        """Get database statistics.

        Returns:
            Dictionary with table counts and sizes
        """
        stats = {}

        # Table counts
        tables = ["api_keys", "users", "logs", "traces", "query_cache", "audit_log"]
        for table in tables:
            result = self.fetch_one(f"SELECT COUNT(*) as count FROM {table}")
            stats[f"{table}_count"] = result[0] if result else 0

        # Database file size
        try:
            stats["db_size_bytes"] = os.path.getsize(self.db_path)
        except OSError:
            stats["db_size_bytes"] = 0

        return stats

    # ============= API KEY METHODS =============

    def create_api_key(
        self,
        key_id: str,
        api_key_hash: str,
        owner_id: str,
        created_at: int,
        expires_at: Optional[int] = None,
        scopes: Optional[str] = None,
        is_active: bool = True,
    ) -> bool:
        """Create a new API key.

        Returns:
            True if created successfully
        """
        sql = """
        INSERT INTO api_keys (key_id, api_key_hash, owner_id, created_at, expires_at, scopes, is_active)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """
        try:
            self.execute(
                sql,
                (
                    key_id,
                    api_key_hash,
                    owner_id,
                    created_at,
                    expires_at,
                    scopes,
                    is_active,
                ),
            )
            return True
        except Exception as e:
            logger.error(f"Failed to create API key: {e}")
            return False

    def validate_api_key(
        self, api_key_hash: str, current_time: int
    ) -> Optional[Dict[str, Any]]:
        """Validate an API key hash.

        Args:
            api_key_hash: Hashed API key
            current_time: Current timestamp

        Returns:
            API key data if valid, None otherwise
        """
        sql = """
        SELECT key_id, owner_id, created_at, expires_at, scopes, is_active
        FROM api_keys
        WHERE api_key_hash = ? AND is_active = true
        """
        try:
            result = self.fetch_one(sql, (api_key_hash,))
            if not result:
                return None

            key_data = dict(result)

            # Check if key is expired
            if key_data.get("expires_at") and key_data["expires_at"] < current_time:
                logger.warning(f"API key {key_data['key_id']} has expired")
                return None

            return key_data
        except Exception as e:
            logger.error(f"Failed to validate API key: {e}")
            return None

    def list_api_keys_by_owner(self, owner_id: str) -> List[Dict[str, Any]]:
        """List all API keys for an owner.

        Args:
            owner_id: Owner user ID

        Returns:
            List of API key data
        """
        sql = """
        SELECT key_id, owner_id, created_at, expires_at, scopes, is_active
        FROM api_keys
        WHERE owner_id = ?
        ORDER BY created_at DESC
        """
        try:
            results = self.fetch_all(sql, (owner_id,))
            return [dict(row) for row in results]
        except Exception as e:
            logger.error(f"Failed to list API keys: {e}")
            return []

    def revoke_api_key(self, key_id: str, owner_id: str) -> bool:
        """Revoke an API key.

        Args:
            key_id: API key ID
            owner_id: Owner user ID

        Returns:
            True if revoked successfully
        """
        sql = "UPDATE api_keys SET is_active = false WHERE key_id = ? AND owner_id = ?"
        try:
            self.execute(sql, (key_id, owner_id))
            return True
        except Exception as e:
            logger.error(f"Failed to revoke API key: {e}")
            return False

    def delete_api_key(self, key_id: str, owner_id: str) -> bool:
        """Delete an API key.

        Args:
            key_id: API key ID
            owner_id: Owner user ID

        Returns:
            True if deleted successfully
        """
        sql = "DELETE FROM api_keys WHERE key_id = ? AND owner_id = ?"
        try:
            self.execute(sql, (key_id, owner_id))
            return True
        except Exception as e:
            logger.error(f"Failed to delete API key: {e}")
            return False

    # ============= USER METHODS =============

    def create_user(
        self,
        user_id: str,
        username: str,
        email: str,
        password_hash: str,
        role: str = "user",
        created_at: int = None,
    ) -> bool:
        """Create a new user.

        Returns:
            True if created successfully
        """
        if created_at is None:
            created_at = int(time.time())

        sql = """
        INSERT INTO users (user_id, username, email, password_hash, roles, is_active, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, true, ?, ?)
        """
        try:
            self.execute(
                sql,
                (user_id, username, email, password_hash, role, created_at, created_at),
            )
            return True
        except Exception as e:
            logger.error(f"Failed to create user: {e}")
            return False

    def get_user_by_id(self, user_id: str) -> Optional[Dict[str, Any]]:
        """Get user by ID.

        Args:
            user_id: User ID

        Returns:
            User data if found
        """
        sql = "SELECT * FROM users WHERE user_id = ?"
        try:
            result = self.fetch_one(sql, (user_id,))
            return dict(result) if result else None
        except Exception as e:
            logger.error(f"Failed to get user by ID: {e}")
            return None

    def get_user_by_username(self, username: str) -> Optional[Dict[str, Any]]:
        """Get user by username.

        Args:
            username: Username

        Returns:
            User data if found
        """
        sql = "SELECT * FROM users WHERE username = ?"
        try:
            result = self.fetch_one(sql, (username,))
            return dict(result) if result else None
        except Exception as e:
            logger.error(f"Failed to get user by username: {e}")
            return None

    def update_user_role(self, user_id: str, role: str) -> bool:
        """Update user role.

        Args:
            user_id: User ID
            role: New role

        Returns:
            True if updated successfully
        """
        sql = "UPDATE users SET roles = ?, updated_at = ? WHERE user_id = ?"
        try:
            self.execute(sql, (role, int(time.time()), user_id))
            return True
        except Exception as e:
            logger.error(f"Failed to update user role: {e}")
            return False

    def delete_user(self, user_id: str) -> bool:
        """Delete a user.

        Args:
            user_id: User ID

        Returns:
            True if deleted successfully
        """
        sql = "DELETE FROM users WHERE user_id = ?"
        try:
            self.execute(sql, (user_id,))
            return True
        except Exception as e:
            logger.error(f"Failed to delete user: {e}")
            return False


# NOTE: There is intentionally no init_service_database()/__main__ helper here.
# Connecting, creating tables, seeding the admin user, and running startup
# cleanup is lifecycle *sequencing* — it belongs to ApplicationLifespan
# (via a LifecycleStep), not to the database module itself. ServiceDatabase
# only knows how to connect, create its own tables, and run queries; it
# doesn't decide when those things happen.
