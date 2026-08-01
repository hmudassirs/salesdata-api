# core/db/adapters/__init__.py
"""Database adapters for different database engines.

These imports are eager (not the lazy __getattr__ pattern used
elsewhere in this codebase) deliberately: importing this package is
what triggers each adapter module's self-registration with the
AdapterRegistry (see the bottom of each adapter file). core/db/session.py
imports this package specifically for that side effect — it never names
DuckDBAdapter/SQLiteAdapter directly; see session.py's _create_adapter().
"""

from core.db.adapters.duckdb import DuckDBAdapter
from core.db.adapters.postgresql import PostgreSQLAdapter
from core.db.adapters.sqlite import SQLiteAdapter

__all__ = [
    "SQLiteAdapter",
    "DuckDBAdapter",
    "PostgreSQLAdapter",
]
