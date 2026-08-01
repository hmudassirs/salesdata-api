"""PostgreSQL database adapter (template for new adapters)."""

from typing import TYPE_CHECKING, Any, Dict, List, Optional

from core.db.logger import get_logger

if TYPE_CHECKING:
    import psycopg2  # type: ignore
else:
    try:
        import psycopg2  # type: ignore
        import psycopg2.extras  # type: ignore
    except ImportError:
        psycopg2 = None  # type: ignore

logger = get_logger(__name__)


class PostgreSQLAdapter:
    """PostgreSQL database adapter for direct connection management.

    This is a template adapter showing how to implement new database adapters.
    """

    def __init__(
        self,
        host: str = "localhost",
        port: int = 5432,
        database: str = "postgres",
        user: str = "postgres",
        password: str = "postgres",
        **extra_options: Any,
    ):
        """Initialize PostgreSQL adapter.

        Args:
            host: Database host
            port: Database port
            database: Database name
            user: Database user
            password: Database password
            **extra_options: Additional connection options
        """
        if psycopg2 is None:
            raise ImportError(
                "psycopg2 package is required. Install with: pip install psycopg2-binary"
            )

        self.host = host
        self.port = port
        self.database = database
        self.user = user
        self.extra_options = extra_options
        self.connection: Optional[Any] = None
        logger.debug(f"PostgreSQL adapter initialized: {user}@{host}:{port}/{database}")

    def connect(self) -> None:
        """Establish database connection."""
        try:
            if psycopg2 is None:
                raise ImportError("psycopg2 is required")

            self.connection = psycopg2.connect(
                host=self.host,
                port=self.port,
                database=self.database,
                user=self.user,
                **self.extra_options,
            )

            # Use RealDictCursor for dict-like rows
            self.connection.cursor_factory = psycopg2.extras.RealDictCursor
            logger.info(
                f"Connected to PostgreSQL: {self.user}@{self.host}:{self.port}/{self.database}"
            )
        except Exception as e:
            logger.error(f"Failed to connect to PostgreSQL: {e}")
            raise

    def disconnect(self) -> None:
        """Close database connection."""
        if self.connection:
            self.connection.close()
            logger.info("Disconnected from PostgreSQL")

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
            with self.connection.cursor() as cursor:
                cursor.execute(sql, params)
                self.connection.commit()
                return cursor.fetchall()
        except Exception as e:
            self.connection.rollback()
            logger.error(f"Query execution failed: {e}")
            raise

    def fetch_all(self, sql: str, params: Optional[List[Any]] = None) -> List[Dict]:
        """Fetch all rows from query.

        Args:
            sql: SQL query string
            params: Query parameters

        Returns:
            List of row dictionaries
        """
        if not self.connection:
            raise RuntimeError("Not connected to database")

        try:
            with self.connection.cursor() as cursor:
                cursor.execute(sql, params)
                rows = cursor.fetchall()
                return [dict(row) for row in rows]
        except Exception as e:
            logger.error(f"Query execution failed: {e}")
            raise

    def fetch_one(self, sql: str, params: Optional[List[Any]] = None) -> Optional[Dict]:
        """Fetch single row from query.

        Args:
            sql: SQL query string
            params: Query parameters

        Returns:
            Single row dictionary or None
        """
        if not self.connection:
            raise RuntimeError("Not connected to database")

        try:
            with self.connection.cursor() as cursor:
                cursor.execute(sql, params)
                row = cursor.fetchone()
                return dict(row) if row else None
        except Exception as e:
            logger.error(f"Query execution failed: {e}")
            raise

    def get_tables(self) -> List[str]:
        """Get list of tables in database.

        Returns:
            List of table names
        """
        if not self.connection:
            raise RuntimeError("Not connected to database")

        sql = """
        SELECT table_name 
        FROM information_schema.tables 
        WHERE table_schema = 'public' 
        ORDER BY table_name
        """

        try:
            rows = self.fetch_all(sql)
            return [row["table_name"] for row in rows]
        except Exception as e:
            logger.error(f"Failed to get tables: {e}")
            raise

    def get_schema(self, table: str) -> List[Dict[str, Any]]:
        """Get table schema information.

        Args:
            table: Table name

        Returns:
            List of column information dictionaries
        """
        if not self.connection:
            raise RuntimeError("Not connected to database")

        sql = """
        SELECT 
            column_name as name, 
            data_type as type, 
            is_nullable,
            column_default as default_value
        FROM information_schema.columns 
        WHERE table_name = %s 
        ORDER BY ordinal_position
        """

        try:
            rows = self.fetch_all(sql, (table,))
            return [dict(row) for row in rows]
        except Exception as e:
            logger.error(f"Failed to get schema for table {table}: {e}")
            raise

    def create_table(
        self,
        table: str,
        columns: Dict[str, str],
        if_not_exists: bool = True,
    ) -> None:
        """Create a table.

        Args:
            table: Table name
            columns: Column definitions {name: type}
            if_not_exists: Only create if table doesn't exist

        Raises:
            RuntimeError: If not connected
        """
        if not self.connection:
            raise RuntimeError("Not connected to database")

        col_defs = ", ".join(
            [f"{name} {col_type}" for name, col_type in columns.items()]
        )
        sql = f"CREATE TABLE {'IF NOT EXISTS' if if_not_exists else ''} {table} ({col_defs})"

        try:
            with self.connection.cursor() as cursor:
                cursor.execute(sql)
                self.connection.commit()
                logger.info(f"Created table: {table}")
        except Exception as e:
            self.connection.rollback()
            logger.error(f"Failed to create table {table}: {e}")
            raise

    def drop_table(self, table: str, if_exists: bool = True) -> None:
        """Drop a table.

        Args:
            table: Table name
            if_exists: Only drop if table exists

        Raises:
            RuntimeError: If not connected
        """
        if not self.connection:
            raise RuntimeError("Not connected to database")

        sql = f"DROP TABLE {'IF EXISTS' if if_exists else ''} {table}"

        try:
            with self.connection.cursor() as cursor:
                cursor.execute(sql)
                self.connection.commit()
                logger.info(f"Dropped table: {table}")
        except Exception as e:
            self.connection.rollback()
            logger.error(f"Failed to drop table {table}: {e}")
            raise


# Self-register with the AdapterRegistry on import. Note PostgreSQLAdapter's
# constructor (host/port/database/user/password) doesn't match
# DuckDBAdapter/SQLiteAdapter's (path, echo) shape — session.py's
# _create_adapter() would need a per-adapter argument-building branch
# (or a `from_config()` classmethod convention) before this adapter could
# actually be selected through the registry the same generic way. The
# registry solves "which class"; constructor argument marshaling for a
# genuinely different-shaped adapter is a separate problem.
from core.db.registry import get_adapter_registry  # noqa: E402

if not get_adapter_registry().is_registered("postgresql"):
    get_adapter_registry().register("postgresql", PostgreSQLAdapter)
