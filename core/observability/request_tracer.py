"""Distributed request tracing, persisted to the service database. Renamed from the old `TracingService` to avoid colliding with core.db.tracing's generic TracingContext/@traced_operation, an unrelated in-process utility."""

import json
import time
from typing import Any, Dict, List, Optional

from core.db.logger import get_logger
from core.storage.service_db import ServiceDatabase

logger = get_logger(__name__)


class RequestTracer:
    """Service for managing distributed traces."""

    def __init__(self, service_db: ServiceDatabase):
        """Initialize tracing service.

        Args:
            service_db: Service database instance
        """
        self.service_db = service_db

    def start_trace(
        self,
        trace_id: str,
        span_id: str,
        operation_name: str,
        service_name: str,
        parent_span_id: str = "",
        user_id: str = "",
        session_id: str = "",
        request_id: str = "",
        service_version: str = "",
        _adapter=None,
    ) -> str:
        """Start a new trace span.

        Args:
            trace_id: Trace ID
            span_id: Span ID
            operation_name: Operation name
            service_name: Service name
            parent_span_id: Parent span ID
            user_id: User ID
            session_id: Session ID
            request_id: Request ID
            service_version: Service version
            _adapter: see RequestLogger.log_event()'s docstring.

        Returns:
            Span ID
        """
        start_time = int(time.time() * 1000000)  # Microseconds

        sql = """
        INSERT INTO traces (
            trace_id, span_id, parent_span_id, operation_name, start_time,
            service_name, service_version, user_id, session_id, request_id
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """

        params = (
            trace_id,
            span_id,
            parent_span_id,
            operation_name,
            start_time,
            service_name,
            service_version,
            user_id,
            session_id,
            request_id,
        )

        try:
            if _adapter is not None:
                self.service_db.execute_on(_adapter, sql, params)
            else:
                self.service_db.execute(sql, params)
            return span_id
        except Exception as e:
            logger.error(f"Failed to start trace: {e}")
            return span_id

    def end_trace(
        self,
        trace_id: str,
        span_id: str,
        status: str = "OK",
        error_message: str = "",
        http_method: str = "",
        http_url: str = "",
        http_status_code: int = 0,
        db_query: str = "",
        db_duration_ms: int = 0,
        tags: Dict[str, Any] = None,
        _adapter=None,
    ) -> None:
        """End a trace span.

        Args:
            trace_id: Trace ID
            span_id: Span ID
            status: Span status
            error_message: Error message if any
            http_method: HTTP method
            http_url: HTTP URL
            http_status_code: HTTP status code
            db_query: Database query
            db_duration_ms: Database query duration
            tags: Additional tags
            _adapter: see RequestLogger.log_event()'s docstring. Used for
                both the read (start_time lookup) and the write here, so
                both happen against the same connection/transaction.
        """
        end_time = int(time.time() * 1000000)  # Microseconds

        # Calculate duration
        read_sql = "SELECT start_time FROM traces WHERE trace_id = ? AND span_id = ?"
        read_params = (trace_id, span_id)
        if _adapter is not None:
            start_result = self.service_db.fetch_one_on(_adapter, read_sql, read_params)
        else:
            start_result = self.service_db.fetch_one(read_sql, read_params)

        duration_ms = 0
        if start_result:
            duration_ms = (
                end_time - start_result[0]
            ) // 1000  # Convert to milliseconds

        tags_json = json.dumps(tags) if tags else ""

        sql = """
        UPDATE traces SET
            end_time = ?, duration_ms = ?, status = ?, error_message = ?,
            http_method = ?, http_url = ?, http_status_code = ?,
            db_query = ?, db_duration_ms = ?, tags = ?
        WHERE trace_id = ? AND span_id = ?
        """

        params = (
            end_time,
            duration_ms,
            status,
            error_message,
            http_method,
            http_url,
            http_status_code,
            db_query,
            db_duration_ms,
            tags_json,
            trace_id,
            span_id,
        )

        try:
            if _adapter is not None:
                self.service_db.execute_on(_adapter, sql, params)
            else:
                self.service_db.execute(sql, params)
        except Exception as e:
            logger.error(f"Failed to end trace: {e}")

    def get_traces(
        self,
        limit: int = 100,
        offset: int = 0,
        service_name: str = "",
        operation_name: str = "",
        user_id: str = "",
        request_id: str = "",
        start_time: int = 0,
        end_time: int = 0,
        status: str = "",
    ) -> List[Dict[str, Any]]:
        """Retrieve traces with filtering.

        Args:
            limit: Maximum number of traces to return
            offset: Offset for pagination
            service_name: Filter by service name
            operation_name: Filter by operation name
            user_id: Filter by user ID
            request_id: Filter by request ID
            start_time: Filter traces after this timestamp
            end_time: Filter traces before this timestamp
            status: Filter by status

        Returns:
            List of trace entries
        """
        conditions = []
        params = []

        if service_name:
            conditions.append("service_name = ?")
            params.append(service_name)

        if operation_name:
            conditions.append("operation_name = ?")
            params.append(operation_name)

        if user_id:
            conditions.append("user_id = ?")
            params.append(user_id)

        if request_id:
            conditions.append("request_id = ?")
            params.append(request_id)

        if start_time:
            conditions.append("start_time >= ?")
            params.append(start_time * 1000000)  # Convert to microseconds

        if end_time:
            conditions.append("start_time <= ?")
            params.append(end_time * 1000000)  # Convert to microseconds

        if status:
            conditions.append("status = ?")
            params.append(status)

        where_clause = " AND ".join(conditions) if conditions else "1=1"

        sql = f"""
        SELECT * FROM traces
        WHERE {where_clause}
        ORDER BY start_time DESC
        LIMIT ? OFFSET ?
        """

        params.extend([limit, offset])

        try:
            rows = self.service_db.fetch_all(sql, tuple(params))
            traces = []
            for row in rows:
                trace_dict = dict(row)
                # Parse tags JSON
                if trace_dict.get("tags"):
                    try:
                        trace_dict["tags"] = json.loads(trace_dict["tags"])
                    except:
                        trace_dict["tags"] = {}
                else:
                    trace_dict["tags"] = {}
                traces.append(trace_dict)
            return traces
        except Exception as e:
            logger.error(f"Failed to retrieve traces: {e}")
            return []
