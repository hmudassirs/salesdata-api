# core/app/api/schemas.py
"""Data models for API requests and responses."""

from typing import List, Optional

from pydantic import BaseModel, ConfigDict, Field


class QueryRequest(BaseModel):
    """Model for SQL query requests."""

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "sql": "SELECT * FROM users LIMIT 10",
                "params": None,
            }
        }
    )

    sql: str = Field(..., description="SQL query to execute")
    params: Optional[list] = Field(None, description="Query parameters")


class QueryResponse(BaseModel):
    """Model for query results."""

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "success": True,
                "data": [{"id": 1, "name": "Alice"}, {"id": 2, "name": "Bob"}],
                "error": None,
                "row_count": 2,
                "cached": False,
            }
        }
    )

    success: bool = Field(..., description="Whether query succeeded")
    data: Optional[list[dict]] = Field(None, description="Query results")
    error: Optional[str] = Field(None, description="Error message if failed")
    row_count: int = Field(0, description="Number of rows returned")
    cached: bool = Field(False, description="Whether result came from cache")
    truncated: bool = Field(
        False,
        description=(
            "True if the result was cut off by max_result_rows/"
            "max_result_bytes -- row_count reflects the truncated size, "
            "not the true result size"
        ),
    )


class HealthResponse(BaseModel):
    """Model for health check responses."""

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "status": "healthy",
                "db_connected": True,
                "pool_metrics": {
                    "max_connections": 10,
                    "active_connections": 2,
                    "idle_connections": 8,
                },
            }
        }
    )

    status: str = Field(..., description="Health status: 'healthy' or 'unhealthy'")
    db_connected: bool = Field(..., description="Whether database is connected")
    pool_metrics: Optional[dict] = Field(None, description="Pool metrics if available")
    executor_metrics: Optional[dict] = Field(
        None,
        description=(
            "Per-executor (db/service/background) active-worker and "
            "approx-queue-depth counts -- roadmap P0-1"
        ),
    )
    query_concurrency_metrics: Optional[dict] = Field(
        None,
        description=(
            "Per-cost-class (fast/normal/expensive) query concurrency "
            "semaphore usage -- roadmap Phase 14"
        ),
    )
    cache_persistence_metrics: Optional[dict] = Field(
        None,
        description=(
            "Bounded background cache-persistence queue depth/throughput "
            "-- roadmap Phase 10"
        ),
    )
    adaptive_sampler_metrics: Optional[dict] = Field(
        None,
        description=(
            "Adaptive performance-sampling rate/escalation state -- "
            "roadmap P1-4 (only present when PERF_ADAPTIVE_SAMPLING=true)"
        ),
    )
    alerts: List[dict] = Field(
        default_factory=list,
        description=(
            "Currently-firing operational alerts, evaluated against pool/"
            "executor/concurrency/persistence/sampling metrics -- roadmap P1-5"
        ),
    )


class TableInfo(BaseModel):
    """Model for table information."""

    table_name: str = Field(..., description="Table name")
    row_count: Optional[int] = Field(None, description="Number of rows")


class TablesResponse(BaseModel):
    """Model for listing tables."""

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "tables": ["users", "orders", "products"],
                "count": 3,
            }
        }
    )

    tables: list[str] = Field(..., description="List of table names")
    count: int = Field(..., description="Number of tables")
