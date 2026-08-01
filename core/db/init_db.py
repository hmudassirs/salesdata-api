"""Initialize database tables including api_keys."""

from pathlib import Path

import duckdb

from core.db.logger import get_logger

logger = get_logger(__name__)


def init_database(db_path: str = "data/db.duckdb") -> None:
    """Initialize database with required tables.

    Args:
        db_path: Path to DuckDB database file
    """
    # Create data directory if it doesn't exist
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)

    conn = duckdb.connect(db_path)

    try:
        # Create api_keys table
        create_api_keys_sql = """
        CREATE TABLE IF NOT EXISTS api_keys (
            key_id VARCHAR PRIMARY KEY,
            api_key_hash VARCHAR NOT NULL,
            owner_id VARCHAR NOT NULL,
            created_at INTEGER NOT NULL,
            expires_at INTEGER,
            scopes VARCHAR,
            is_active BOOLEAN DEFAULT true
        );
        """
        conn.execute(create_api_keys_sql)
        logger.info("Created api_keys table")

        # Create users table (for api key ownership)
        create_users_sql = """
        CREATE TABLE IF NOT EXISTS users (
            user_id VARCHAR PRIMARY KEY,
            username VARCHAR NOT NULL UNIQUE,
            email VARCHAR NOT NULL UNIQUE,
            password_hash VARCHAR NOT NULL,
            roles VARCHAR DEFAULT 'viewer',
            is_active BOOLEAN DEFAULT true,
            created_at INTEGER NOT NULL,
            updated_at INTEGER NOT NULL
        );
        """
        conn.execute(create_users_sql)
        logger.info("Created users table")

        # Create sample data
        check_admin = conn.execute(
            "SELECT COUNT(*) as cnt FROM users WHERE username = 'admin'"
        ).fetchone()

        if check_admin[0] == 0:
            conn.execute("""
                INSERT INTO users (user_id, username, email, password_hash, roles, created_at, updated_at)
                VALUES (
                    'user_admin_001',
                    'admin',
                    'admin@example.com',
                    'hashed_password_placeholder',
                    'admin,editor,viewer',
                    1707000000,
                    1707000000
                );
            """)
            logger.info("Added admin user")

        conn.commit()
        logger.info("Database initialized successfully")

    except Exception as e:
        logger.error(f"Error initializing database: {e}")
        raise
    finally:
        conn.close()


if __name__ == "__main__":
    init_database()
