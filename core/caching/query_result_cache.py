"""Persistent cache of /api/query SELECT results. Renamed from the old `QueryCacheService` to avoid colliding with core.db.cache's LRU/TTL in-memory QueryCache classes, an unrelated in-process utility with the exact same 'cache' name."""

import datetime
import decimal
import hashlib
import json
import time
from typing import Any, Dict, List, Optional

from core.db.logger import get_logger
from core.storage.service_db import ServiceDatabase

logger = get_logger(__name__)


def _json_default(value: Any) -> Any:
    """`json.dumps(default=...)` hook for types SQL drivers commonly
    hand back that the stdlib encoder doesn't know: dates/times (ISO
    format) and Decimals (as float, since cached results are read-only
    display data, not something re-parsed for further arithmetic).
    Without this, caching the result of any query touching a DATE,
    TIMESTAMP, or DECIMAL/NUMERIC column raised "Object of type date is
    not JSON serializable" out of `cache_result()` (and, since that call
    used to happen inline, out of whatever background task called it)."""
    if isinstance(value, (datetime.date, datetime.datetime, datetime.time)):
        return value.isoformat()
    if isinstance(value, decimal.Decimal):
        return float(value)
    if isinstance(value, (bytes, bytearray)):
        return value.decode("utf-8", errors="replace")
    raise TypeError(f"Object of type {type(value).__name__} is not JSON serializable")


class QueryResultCache:
    """Service for caching query results."""

    def __init__(self, service_db: ServiceDatabase):
        """Initialize query cache service.

        Args:
            service_db: Service database instance
        """
        self.service_db = service_db

    def generate_cache_key(self, query_sql: str, params: tuple = ()) -> str:
        """Generate a cache key for a query.

        Args:
            query_sql: SQL query string
            params: Query parameters

        Returns:
            Cache key
        """
        # Create a hash of query + params
        query_str = f"{query_sql}|{str(params)}"
        return hashlib.sha256(query_str.encode()).hexdigest()

    def get_cached_result(self, cache_key: str) -> Optional[Dict[str, Any]]:
        """Get cached query result.

        Note: this used to also issue a synchronous
        `UPDATE query_cache SET last_accessed_at ...` on every hit,
        inline with the read. That makes every cache *hit* pay for a
        SQLite write before the caller can proceed -- exactly the
        "cache-hit traffic generating synchronous writes" the roadmap
        calls out (section 3, rule #3). Call `record_access()`
        separately (ideally fire-and-forget, off the request path) if
        you want that bookkeeping.

        Args:
            cache_key: Cache key

        Returns:
            Cached result or None if not found/expired
        """
        current_time = int(time.time())

        sql = """
        SELECT * FROM query_cache
        WHERE cache_key = ? AND (expires_at IS NULL OR expires_at > ?)
        """

        try:
            result = self.service_db.fetch_one(sql, (cache_key, current_time))
            return dict(result) if result else None
        except Exception as e:
            logger.error(f"Failed to get cached result: {e}")
            return None

    def record_access(self, cache_key: str) -> None:
        """Record a cache hit's access statistics. Split out of
        `get_cached_result()` so callers can run this as fire-and-forget
        background work instead of blocking the response on it -- see
        that method's docstring."""
        try:
            current_time = int(time.time())
            self.service_db.execute(
                "UPDATE query_cache SET last_accessed_at = ?, access_count = access_count + 1 WHERE cache_key = ?",
                (current_time, cache_key),
            )
        except Exception as e:
            logger.error(f"Failed to record cache access: {e}")

    def cache_result(
        self,
        query_sql: str,
        result_data: List[Dict[str, Any]],
        params: tuple = (),
        user_id: str = "",
        session_id: str = "",
        execution_time_ms: int = 0,
        ttl_seconds: int = 3600,  # 1 hour default
    ) -> str:
        """Cache query result.

        Args:
            query_sql: SQL query string
            result_data: Query result data
            params: Query parameters
            user_id: User ID
            session_id: Session ID
            execution_time_ms: Query execution time
            ttl_seconds: Time to live in seconds

        Returns:
            Cache key
        """
        cache_key = self.generate_cache_key(query_sql, params)
        query_hash = hashlib.sha256(query_sql.encode()).hexdigest()
        created_at = int(time.time())
        expires_at = created_at + ttl_seconds if ttl_seconds > 0 else None

        # Serialize result data. `default=_json_default` covers date/
        # datetime/Decimal/bytes values that raw DB rows commonly
        # contain and that json can't serialize on its own — this is
        # what was throwing "Object of type date is not JSON
        # serializable" here.
        result_json = json.dumps(result_data, default=_json_default)
        result_size = len(result_json.encode())

        sql = """
        INSERT OR REPLACE INTO query_cache (
            cache_key, query_hash, query_sql, result_data, result_count,
            created_at, expires_at, user_id, session_id, execution_time_ms, result_size_bytes
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """

        try:
            self.service_db.execute(
                sql,
                (
                    cache_key,
                    query_hash,
                    query_sql,
                    result_json,
                    len(result_data),
                    created_at,
                    expires_at,
                    user_id,
                    session_id,
                    execution_time_ms,
                    result_size,
                ),
            )
            return cache_key
        except Exception as e:
            logger.error(f"Failed to cache result: {e}")
            return cache_key

    def clear_all(self) -> int:
        """Delete every cached entry, unconditionally.

        `invalidate_cache()` requires at least a pattern or user_id and
        refuses to run with neither (by design -- an accidental
        empty-args call there shouldn't wipe the whole cache). This is
        the explicit, intentional "wipe everything" used after a write
        statement executes, since QueryCacheCoordinator has no way to
        know precisely which cached SELECTs a given INSERT/UPDATE/
        DELETE/DDL statement could have affected (roadmap 16.3: "avoid
        stale data after mutations" -- correctness over precision).
        """
        try:
            result = self.service_db.execute("DELETE FROM query_cache", ())
            deleted_count = result.rowcount
            logger.info(f"Cleared entire query cache ({deleted_count} entries) after a write statement")
            return deleted_count
        except Exception as e:
            logger.error(f"Failed to clear cache: {e}")
            return 0

    def invalidate_cache(self, query_pattern: str = "", user_id: str = "") -> int:
        """Invalidate cache entries.

        Args:
            query_pattern: SQL pattern to match (using LIKE)
            user_id: User ID to invalidate cache for

        Returns:
            Number of entries invalidated
        """
        conditions = []
        params = []

        if query_pattern:
            conditions.append("query_sql LIKE ?")
            params.append(f"%{query_pattern}%")

        if user_id:
            conditions.append("user_id = ?")
            params.append(user_id)

        if not conditions:
            return 0

        where_clause = " AND ".join(conditions)

        sql = f"DELETE FROM query_cache WHERE {where_clause}"

        try:
            result = self.service_db.execute(sql, tuple(params))
            deleted_count = result.rowcount
            logger.info(f"Invalidated {deleted_count} cache entries")
            return deleted_count
        except Exception as e:
            logger.error(f"Failed to invalidate cache: {e}")
            return 0

    def get_cache_stats(self) -> Dict[str, Any]:
        """Get cache statistics.

        Returns:
            Cache statistics
        """
        try:
            # Total entries
            total_result = self.service_db.fetch_one("SELECT COUNT(*) FROM query_cache")
            total_entries = total_result[0] if total_result else 0

            # Active entries (not expired)
            current_time = int(time.time())
            active_result = self.service_db.fetch_one(
                "SELECT COUNT(*) FROM query_cache WHERE expires_at IS NULL OR expires_at > ?",
                (current_time,),
            )
            active_entries = active_result[0] if active_result else 0

            # Total size
            size_result = self.service_db.fetch_one(
                "SELECT SUM(result_size_bytes) FROM query_cache"
            )
            total_size = size_result[0] if size_result else 0

            # Hit statistics
            hit_result = self.service_db.fetch_one(
                "SELECT SUM(access_count) FROM query_cache"
            )
            total_hits = hit_result[0] if hit_result else 0

            return {
                "total_entries": total_entries,
                "active_entries": active_entries,
                "expired_entries": total_entries - active_entries,
                "total_size_bytes": total_size,
                "total_access_count": total_hits,
                "average_size_bytes": total_size // max(total_entries, 1),
            }
        except Exception as e:
            logger.error(f"Failed to get cache stats: {e}")
            return {}
