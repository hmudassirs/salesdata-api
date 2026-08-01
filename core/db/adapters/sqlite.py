"""SQLite database adapter (SOLID - Single Responsibility)."""

import sqlite3
from typing import List, Optional

from core.db.logger import get_logger

logger = get_logger(__name__)
sql_logger = get_logger("core.db.adapters.sql")


class SQLiteAdapter:
    """SQLite database adapter for direct connection management."""

    def __init__(self, db_path: str, echo: bool = False):
        """Initialize SQLite adapter.

        Args:
            db_path: Path to SQLite database file
            echo: If True, SQL statements will be logged to the dedicated SQL logger
        """
        self.db_path = db_path
        self.connection: Optional[sqlite3.Connection] = None
        self.echo = echo

    def connect(self, timeout: int = 30, check_same_thread: bool = False) -> None:
        """Establish database connection.

        Args:
            timeout: Connection timeout in seconds
            check_same_thread: Allow access from different threads
        """
        try:
            self.connection = sqlite3.connect(
                self.db_path,
                timeout=timeout,
                check_same_thread=check_same_thread,
            )
            self.connection.row_factory = sqlite3.Row
            logger.info(f"Connected to SQLite database: {self.db_path}")
        except sqlite3.Error as e:
            logger.error(f"Failed to connect to SQLite: {e}")
            raise

    def disconnect(self) -> None:
        """Close database connection."""
        if self.connection:
            self.connection.close()
            logger.info("Disconnected from SQLite database")

    def execute(self, sql: str, params: tuple = ()) -> sqlite3.Cursor:
        """Execute SQL query.

        Args:
            sql: SQL query string
            params: Query parameters

        Returns:
            Cursor object

        Raises:
            RuntimeError: If not connected
        """
        if not self.connection:
            raise RuntimeError("Not connected to database")

        try:
            if self.echo:
                sql_logger.info("SQL EXECUTE: %s -- params=%s", sql, params)
            cursor = self.connection.cursor()
            cursor.execute(sql, params)
            return cursor
        except sqlite3.Error as e:
            logger.error(f"Query execution failed: {e}")
            raise

    def execute_many(self, sql: str, params_list: List[tuple]) -> None:
        """Execute multiple SQL queries.

        Args:
            sql: SQL query string
            params_list: List of parameter tuples

        Raises:
            RuntimeError: If not connected
        """
        if not self.connection:
            raise RuntimeError("Not connected to database")

        try:
            if self.echo:
                sql_logger.info(
                    "SQL EXECUTEMANY: %s -- batches=%d", sql, len(params_list)
                )
            cursor = self.connection.cursor()
            cursor.executemany(sql, params_list)
            self.connection.commit()
            logger.info(f"Executed {len(params_list)} queries")
        except sqlite3.Error as e:
            logger.error(f"Batch execution failed: {e}")
            self.connection.rollback()
            raise

    def fetch_one(self, sql: str, params: tuple = ()) -> Optional[dict]:
        """Fetch single row.

        Args:
            sql: SQL query string
            params: Query parameters

        Returns:
            Single row as dictionary or None
        """
        cursor = self.execute(sql, params)
        row = cursor.fetchone()
        return dict(row) if row else None

    def fetch_all(self, sql: str, params: tuple = ()) -> List[dict]:
        """Fetch all rows.

        Args:
            sql: SQL query string
            params: Query parameters

        Returns:
            List of rows as dictionaries
        """
        cursor = self.execute(sql, params)
        rows = cursor.fetchall()
        return [dict(row) for row in rows]

    def insert(self, table: str, data: dict) -> int:
        """Insert record.

        Args:
            table: Table name
            data: Column-value dictionary

        Returns:
            Last inserted row ID
        """
        columns = ", ".join(data.keys())
        placeholders = ", ".join(["?"] * len(data))
        sql = f"INSERT INTO {table} ({columns}) VALUES ({placeholders})"

        cursor = self.execute(sql, tuple(data.values()))
        if self.connection:
            self.connection.commit()
        return cursor.lastrowid or 0

    def update(
        self, table: str, data: dict, where: str = "", params: tuple = ()
    ) -> int:
        """Update records.

        Args:
            table: Table name
            data: Column-value dictionary
            where: WHERE clause
            params: WHERE clause parameters

        Returns:
            Number of rows affected
        """
        set_clause = ", ".join([f"{k} = ?" for k in data.keys()])
        sql = f"UPDATE {table} SET {set_clause}"

        if where:
            sql += f" WHERE {where}"

        all_params = tuple(data.values()) + params
        cursor = self.execute(sql, all_params)
        if self.connection:
            self.connection.commit()
        return cursor.rowcount

    def delete(self, table: str, where: str = "", params: tuple = ()) -> int:
        """Delete records.

        Args:
            table: Table name
            where: WHERE clause
            params: WHERE clause parameters

        Returns:
            Number of rows affected
        """
        sql = f"DELETE FROM {table}"

        if where:
            sql += f" WHERE {where}"

        cursor = self.execute(sql, params)
        if self.connection:
            self.connection.commit()
        return cursor.rowcount

    def get_tables(self) -> List[str]:
        """Get list of tables.

        Returns:
            List of table names
        """
        sql = "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
        rows = self.fetch_all(sql)
        return [row["name"] for row in rows]

    def get_schema(self, table: str) -> List[dict]:
        """Get table schema.

        Args:
            table: Table name

        Returns:
            List of column information
        """
        sql = f"PRAGMA table_info({table})"
        rows = self.fetch_all(sql)
        return [dict(row) for row in rows]

    def vacuum(self) -> None:
        """Optimize database."""
        try:
            self.execute("VACUUM")
            if self.connection:
                self.connection.commit()
            logger.info("Database vacuumed")
        except sqlite3.Error as e:
            logger.error(f"Vacuum failed: {e}")
            raise

    def create_backup(self, backup_path: str) -> None:
        """Create database backup.

        Args:
            backup_path: Backup file path
        """
        if not self.connection:
            raise RuntimeError("Not connected to database")

        try:
            backup_conn = sqlite3.connect(backup_path)
            self.connection.backup(backup_conn)
            backup_conn.close()
            logger.info(f"Backup created: {backup_path}")
        except sqlite3.Error as e:
            logger.error(f"Backup failed: {e}")
            raise


# Self-register with the AdapterRegistry on import — see duckdb.py's
# matching block for why this is the correct call shape.
from core.db.registry import get_adapter_registry  # noqa: E402

if not get_adapter_registry().is_registered("sqlite"):
    get_adapter_registry().register("sqlite", SQLiteAdapter)
