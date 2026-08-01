"""Application/request event logging, persisted to the service database."""

import time
from typing import Any, Dict, List, Optional

from core.db.logger import get_logger
from core.storage.service_db import ServiceDatabase

logger = get_logger(__name__)


class RequestLogger:
    """Service for managing application logs."""

    def __init__(self, service_db: ServiceDatabase):
        """Initialize logging service.

        Args:
            service_db: Service database instance
        """
        self.service_db = service_db

    def log_event(
        self,
        level: str,
        message: str,
        logger_name: str = "",
        module: str = "",
        function: str = "",
        line: int = 0,
        exception: str = "",
        user_id: str = "",
        session_id: str = "",
        request_id: str = "",
        ip_address: str = "",
        user_agent: str = "",
        _adapter=None,
    ) -> None:
        """Log an application event.

        Args:
            level: Log level (DEBUG, INFO, WARNING, ERROR, CRITICAL)
            message: Log message
            logger_name: Logger name
            module: Module name
            function: Function name
            line: Line number
            exception: Exception traceback
            user_id: User ID
            session_id: Session ID
            request_id: Request ID
            ip_address: Client IP address
            user_agent: User agent string
            _adapter: Optional pre-acquired connection (from
                ServiceDatabase.transaction()) to run on instead of
                acquiring/committing a new one — see
                emit_request_observability(), which batches this call
                together with tracing/audit into one transaction.
        """
        timestamp = int(time.time())

        sql = """
        INSERT INTO logs (
            timestamp, level, logger, message, module, function, line,
            exception, user_id, session_id, request_id, ip_address, user_agent
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """

        params = (
            timestamp,
            level,
            logger_name,
            message,
            module,
            function,
            line,
            exception,
            user_id,
            session_id,
            request_id,
            ip_address,
            user_agent,
        )

        try:
            if _adapter is not None:
                self.service_db.execute_on(_adapter, sql, params)
            else:
                self.service_db.execute(sql, params)
        except Exception as e:
            # Don't let logging failures crash the application
            print(f"Failed to log event: {e}")

    def get_logs(
        self,
        limit: int = 100,
        offset: int = 0,
        level: str = "",
        user_id: str = "",
        request_id: str = "",
        start_time: int = 0,
        end_time: int = 0,
    ) -> List[Dict[str, Any]]:
        """Retrieve logs with filtering.

        Args:
            limit: Maximum number of logs to return
            offset: Offset for pagination
            level: Filter by log level
            user_id: Filter by user ID
            request_id: Filter by request ID
            start_time: Filter logs after this timestamp
            end_time: Filter logs before this timestamp

        Returns:
            List of log entries
        """
        conditions = []
        params = []

        if level:
            conditions.append("level = ?")
            params.append(level)

        if user_id:
            conditions.append("user_id = ?")
            params.append(user_id)

        if request_id:
            conditions.append("request_id = ?")
            params.append(request_id)

        if start_time:
            conditions.append("timestamp >= ?")
            params.append(start_time)

        if end_time:
            conditions.append("timestamp <= ?")
            params.append(end_time)

        where_clause = " AND ".join(conditions) if conditions else "1=1"

        sql = f"""
        SELECT * FROM logs
        WHERE {where_clause}
        ORDER BY timestamp DESC
        LIMIT ? OFFSET ?
        """

        params.extend([limit, offset])

        try:
            rows = self.service_db.fetch_all(sql, tuple(params))
            return [dict(row) for row in rows]
        except Exception as e:
            logger.error(f"Failed to retrieve logs: {e}")
            return []

    def log_request(
        self,
        method: str,
        url: str,
        status_code: int,
        duration_ms: float,
        user_id: Optional[str] = None,
        ip_address: str = "",
        user_agent: str = "",
        request_id: Optional[str] = None,
        session_id: Optional[str] = None,
        error: Optional[str] = None,
        _adapter=None,
    ) -> None:
        """Log an HTTP request.

        Args:
            method: HTTP method
            url: Request URL
            status_code: HTTP status code
            duration_ms: Request duration in milliseconds
            user_id: User ID if authenticated
            ip_address: Client IP address
            user_agent: User agent string
            request_id: Request ID for tracing
            error: Error message if request failed
            _adapter: see log_event()'s docstring.
        """
        level = "ERROR" if status_code >= 400 else "INFO"
        message = f"{method} {url} - {status_code}"

        if error:
            message += f" - Error: {error}"

        self.log_event(
            level=level,
            message=message,
            logger_name="http.request",
            module="api",
            function="middleware",
            user_id=user_id or "",
            request_id=request_id or "",
            session_id=session_id or "",
            ip_address=ip_address,
            user_agent=user_agent,
            _adapter=_adapter,
        )
