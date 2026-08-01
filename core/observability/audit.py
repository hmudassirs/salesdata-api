"""Security/compliance audit trail (who did what, when), persisted to the service database."""

import json
import time
from typing import Any, Dict, List, Optional

from core.db.logger import get_logger
from core.storage.service_db import ServiceDatabase

logger = get_logger(__name__)


class AuditTrail:
    """Service for managing audit logs."""

    def __init__(self, service_db: ServiceDatabase):
        """Initialize audit service.

        Args:
            service_db: Service database instance
        """
        self.service_db = service_db

    def log_audit_event(
        self,
        event_type: str,
        action: str,
        user_id: str = "",
        session_id: str = "",
        ip_address: str = "",
        user_agent: str = "",
        resource_type: str = "",
        resource_id: str = "",
        old_values: Dict[str, Any] = None,
        new_values: Dict[str, Any] = None,
        success: bool = True,
        error_message: str = "",
        metadata: Dict[str, Any] = None,
        _adapter=None,
    ) -> None:
        """Log an audit event.

        Args:
            event_type: Type of event (e.g., 'auth', 'api_key', 'user')
            action: Action performed (e.g., 'login', 'create', 'delete')
            user_id: User ID performing the action
            session_id: Session ID
            ip_address: Client IP address
            user_agent: User agent string
            resource_type: Type of resource affected
            resource_id: ID of resource affected
            old_values: Previous values (for updates)
            new_values: New values (for updates/creates)
            success: Whether the action succeeded
            error_message: Error message if failed
            metadata: Additional metadata
            _adapter: see RequestLogger.log_event()'s docstring.
        """
        timestamp = int(time.time())

        old_values_json = json.dumps(old_values) if old_values else None
        new_values_json = json.dumps(new_values) if new_values else None
        metadata_json = json.dumps(metadata) if metadata else None

        sql = """
        INSERT INTO audit_log (
            timestamp, event_type, user_id, session_id, ip_address, user_agent,
            resource_type, resource_id, action, old_values, new_values,
            success, error_message, metadata
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """

        params = (
            timestamp,
            event_type,
            user_id,
            session_id,
            ip_address,
            user_agent,
            resource_type,
            resource_id,
            action,
            old_values_json,
            new_values_json,
            success,
            error_message,
            metadata_json,
        )

        try:
            if _adapter is not None:
                self.service_db.execute_on(_adapter, sql, params)
            else:
                self.service_db.execute(sql, params)
        except Exception as e:
            logger.error(f"Failed to log audit event: {e}")

    def get_audit_events(
        self,
        limit: int = 100,
        offset: int = 0,
        event_type: str = "",
        user_id: str = "",
        resource_type: str = "",
        resource_id: str = "",
        action: str = "",
        start_time: int = 0,
        end_time: int = 0,
        success: Optional[bool] = None,
    ) -> List[Dict[str, Any]]:
        """Retrieve audit events with filtering.

        Args:
            limit: Maximum number of events to return
            offset: Offset for pagination
            event_type: Filter by event type
            user_id: Filter by user ID
            resource_type: Filter by resource type
            resource_id: Filter by resource ID
            action: Filter by action
            start_time: Filter events after this timestamp
            end_time: Filter events before this timestamp
            success: Filter by success status

        Returns:
            List of audit events
        """
        conditions = []
        params = []

        if event_type:
            conditions.append("event_type = ?")
            params.append(event_type)

        if user_id:
            conditions.append("user_id = ?")
            params.append(user_id)

        if resource_type:
            conditions.append("resource_type = ?")
            params.append(resource_type)

        if resource_id:
            conditions.append("resource_id = ?")
            params.append(resource_id)

        if action:
            conditions.append("action = ?")
            params.append(action)

        if start_time:
            conditions.append("timestamp >= ?")
            params.append(start_time)

        if end_time:
            conditions.append("timestamp <= ?")
            params.append(end_time)

        if success is not None:
            conditions.append("success = ?")
            params.append(success)

        where_clause = " AND ".join(conditions) if conditions else "1=1"

        sql = f"""
        SELECT * FROM audit_log
        WHERE {where_clause}
        ORDER BY timestamp DESC
        LIMIT ? OFFSET ?
        """

        params.extend([limit, offset])

        try:
            rows = self.service_db.fetch_all(sql, tuple(params))
            events = []
            for row in rows:
                event_dict = dict(row)
                # Parse JSON fields
                for field in ["old_values", "new_values", "metadata"]:
                    if event_dict.get(field):
                        try:
                            event_dict[field] = json.loads(event_dict[field])
                        except:
                            event_dict[field] = None
                    else:
                        event_dict[field] = None
                events.append(event_dict)
            return events
        except Exception as e:
            logger.error(f"Failed to retrieve audit events: {e}")
            return []
