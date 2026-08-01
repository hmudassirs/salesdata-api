"""Persistent /api/query result cache (SQLite-backed).

Renamed from service_manager.py's QueryCacheService to QueryResultCache
to stop colliding with core.db.cache's LRU/TTL in-memory QueryCache
classes — same word, two unrelated caches.
"""

from core.caching.query_result_cache import QueryResultCache

__all__ = ["QueryResultCache"]
