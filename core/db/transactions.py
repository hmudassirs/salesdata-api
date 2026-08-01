"""Pool-level transaction helpers.

Note: For comprehensive transaction support with ACID guarantees,
use core.db.transactions (TransactionAwareSession, TransactionManager).

This module provides simple context managers for pool-based transactions.
"""

from contextlib import asynccontextmanager, contextmanager
from typing import Any


@contextmanager
def transaction(pool: Any):
    """Synchronous transaction context manager for connection pool.

    Args:
        pool: Connection pool instance

    Yields:
        Connection object within transaction

    Example:
        with transaction(pool) as conn:
            conn.execute("INSERT INTO table VALUES (...)")
            # Auto-commits on exit, rolls back on exception
    """
    conn = pool.acquire()
    try:
        if hasattr(conn, "execute"):
            conn.execute("BEGIN")
        yield conn
        if hasattr(conn, "execute"):
            conn.execute("COMMIT")
    except Exception:
        if hasattr(conn, "execute"):
            try:
                conn.execute("ROLLBACK")
            except Exception:
                pass
        raise
    finally:
        pool.release(conn)


@asynccontextmanager
async def async_transaction(pool: Any):
    """Asynchronous transaction context manager for connection pool.

    Args:
        pool: Async connection pool instance

    Yields:
        Connection object within transaction

    Example:
        async with async_transaction(pool) as conn:
            await conn.execute("INSERT INTO table VALUES (...)")
            # Auto-commits on exit, rolls back on exception
    """
    conn = await pool.acquire()
    try:
        if hasattr(conn, "execute"):
            await conn.execute("BEGIN")
        yield conn
        if hasattr(conn, "execute"):
            await conn.execute("COMMIT")
    except Exception:
        if hasattr(conn, "execute"):
            try:
                await conn.execute("ROLLBACK")
            except Exception:
                pass
        raise
    finally:
        await pool.release(conn)
