"""DuckDB database adapter (SOLID - Single Responsibility)."""

from typing import TYPE_CHECKING, Any, Dict, List, Optional

from core.db.logger import get_logger

if TYPE_CHECKING:
    import duckdb  # type: ignore
else:
    try:
        import duckdb  # type: ignore
    except ImportError:
        duckdb = None  # type: ignore

logger = get_logger(__name__)
sql_logger = get_logger("core.db.adapters.sql")


class DuckDBAdapter:
    """DuckDB database adapter for direct connection management."""

    def __init__(self, db_path: str = ":memory:", echo: bool = False):
        """Initialize DuckDB adapter.

        Args:
            db_path: Path to DuckDB database file or :memory: for in-memory
        """
        if duckdb is None:
            raise ImportError(
                "duckdb package is required. Install with: pip install duckdb"
            )

        self.db_path = db_path
        self.connection: Optional[Any] = None
        self.echo = echo

    def connect(self) -> None:
        """Establish database connection."""
        try:
            if duckdb is None:
                raise ImportError(
                    "duckdb is required. Install with: pip install duckdb"
                )
            self.connection = duckdb.connect(self.db_path)  # type: ignore
            logger.info(f"Connected to DuckDB: {self.db_path}")
        except Exception as e:
            logger.error(f"Failed to connect to DuckDB: {e}")
            raise

    def disconnect(self) -> None:
        """Close database connection."""
        if self.connection:
            self.connection.close()
            logger.info("Disconnected from DuckDB")

    def execute(self, sql: str, params: Optional[List[Any]] = None) -> Any:
        """Execute SQL query.

        Args:
            sql: SQL query string
            params: Query parameters

        Returns:
            Query result

        Raises:
            RuntimeError: If not connected
        """
        if not self.connection:
            raise RuntimeError("Not connected to database")

        try:
            if self.echo:
                sql_logger.info("SQL EXECUTE: %s -- params=%s", sql, params)
            if params:
                return self.connection.execute(sql, params).fetchall()
            return self.connection.execute(sql).fetchall()
        except Exception as e:
            logger.error(f"Query execution failed: {e}")
            raise

    def execute_query(self, sql: str, params: Optional[List[Any]] = None) -> Any:
        """Execute SQL query and return result.

        Args:
            sql: SQL query string
            params: Query parameters

        Returns:
            Query result object
        """
        if not self.connection:
            raise RuntimeError("Not connected to database")

        try:
            if self.echo:
                sql_logger.info("SQL QUERY: %s -- params=%s", sql, params)
            if params:
                return self.connection.execute(sql, params)
            return self.connection.execute(sql)
        except Exception as e:
            logger.error(f"Query execution failed: {e}")
            raise

    def fetch_one(self, sql: str, params: Optional[List[Any]] = None) -> Optional[Dict]:
        """Fetch single row.

        Args:
            sql: SQL query string
            params: Query parameters

        Returns:
            Single row as dictionary or None
        """
        result = self.execute_query(sql, params).fetchone()
        if result is None:
            return None
        # Handle both tuple and dict-like results
        if isinstance(result, dict):
            return result
        # Get column names from the result
        query_result = self.execute_query(sql, params)
        columns = (
            [desc[0] for desc in query_result.description]
            if hasattr(query_result, "description")
            else []
        )
        if columns and isinstance(result, tuple):
            return dict(zip(columns, result))
        return None

    def fetch_all(self, sql: str, params: Optional[List[Any]] = None) -> List[Dict]:
        """Fetch all rows.

        Args:
            sql: SQL query string
            params: Query parameters

        Returns:
            List of rows as dictionaries
        """
        query_result = self.execute_query(sql, params)
        results = query_result.fetchall()

        # Handle DuckDB's native dict-like results
        if not results:
            return []

        # Check if results are already dict-like
        if isinstance(results[0], dict):
            return results

        # Get column names and convert tuples to dicts
        try:
            columns = (
                [desc[0] for desc in query_result.description]
                if hasattr(query_result, "description")
                else []
            )
            if columns:
                return [
                    dict(zip(columns, row)) if isinstance(row, tuple) else row
                    for row in results
                ]
        except (AttributeError, IndexError):
            pass

        return results

    def insert(self, table: str, data: Dict[str, Any]) -> int:
        """Insert record.

        Args:
            table: Table name
            data: Column-value dictionary

        Returns:
            Number of rows inserted
        """
        columns = ", ".join(data.keys())
        placeholders = ", ".join(["?"] * len(data))
        sql = f"INSERT INTO {table} ({columns}) VALUES ({placeholders})"

        result = self.execute_query(sql, list(data.values()))
        return result.rowcount if hasattr(result, "rowcount") else 1

    def update(
        self,
        table: str,
        data: Dict[str, Any],
        where: str = "",
        params: Optional[List[Any]] = None,
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

        all_params = list(data.values())
        if where:
            sql += f" WHERE {where}"
            if params:
                all_params.extend(params)

        result = self.execute_query(sql, all_params)
        return result.rowcount if hasattr(result, "rowcount") else 0

    def delete(
        self,
        table: str,
        where: str = "",
        params: Optional[List[Any]] = None,
    ) -> int:
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

        result = self.execute_query(sql, params or [])
        return result.rowcount if hasattr(result, "rowcount") else 0

    def get_tables(self) -> List[str]:
        """Get list of tables.

        Returns:
            List of table names
        """
        sql = "SELECT table_name FROM information_schema.tables WHERE table_schema = 'main'"
        rows = self.fetch_all(sql)
        return [row["table_name"] for row in rows]

    def get_schema(self, table: str) -> List[Dict]:
        """Get table schema.

        Args:
            table: Table name

        Returns:
            List of column information
        """
        sql = f"PRAGMA table_info({table})"
        return self.fetch_all(sql)

    def create_table(self, sql: str) -> None:
        """Create table.

        Args:
            sql: CREATE TABLE statement
        """
        try:
            self.execute_query(sql)
            logger.info("Table created")
        except Exception as e:
            logger.error(f"Table creation failed: {e}")
            raise

    def drop_table(self, table: str, if_exists: bool = True) -> None:
        """Drop table.

        Args:
            table: Table name
            if_exists: Only drop if exists
        """
        try:
            if_clause = "IF EXISTS" if if_exists else ""
            sql = f"DROP TABLE {if_clause} {table}"
            self.execute_query(sql)
            logger.info(f"Table dropped: {table}")
        except Exception as e:
            logger.error(f"Table drop failed: {e}")
            raise

    def export_to_csv(self, table: str, output_path: str) -> None:
        """Export table to CSV.

        Args:
            table: Table name
            output_path: Output file path
        """
        try:
            sql = (
                f"COPY (SELECT * FROM {table}) TO '{output_path}' (FORMAT CSV, HEADER)"
            )
            self.execute_query(sql)
            logger.info(f"Table exported to CSV: {output_path}")
        except Exception as e:
            logger.error(f"CSV export failed: {e}")
            raise

    def import_from_csv(
        self, table: str, csv_path: str, options: Optional[str] = None
    ) -> None:
        """Import CSV to table.

        Args:
            table: Table name
            csv_path: CSV file path
            options: Import options
        """
        try:
            opts = f" ({options})" if options else ""
            sql = f"COPY {table} FROM '{csv_path}' (FORMAT CSV, HEADER){opts}"
            self.execute_query(sql)
            logger.info(f"Data imported from CSV: {csv_path}")
        except Exception as e:
            logger.error(f"CSV import failed: {e}")
            raise

    def get_database_info(self) -> Dict[str, Any]:
        """Get database information.

        Returns:
            Dictionary with database info
        """
        return {
            "path": self.db_path,
            "tables": self.get_tables(),
            "version": duckdb.__version__ if duckdb else "unknown",
        }


# Self-register with the AdapterRegistry on import. This is the piece
# that makes core/db/session.py's _create_adapter() work as a plain
# name -> class lookup instead of a hardcoded if/else that has to be
# edited every time an adapter is added. Uses the module-level singleton
# (get_adapter_registry()), not a bare AdapterRegistry.register(...)
# class-level call — register() is an instance method on AdapterRegistry,
# not static, so calling it on the class itself would raise TypeError.
from core.db.registry import get_adapter_registry  # noqa: E402

if not get_adapter_registry().is_registered("duckdb"):
    get_adapter_registry().register("duckdb", DuckDBAdapter)
