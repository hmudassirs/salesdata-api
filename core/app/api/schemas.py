# core/app/api/schemas.py
"""Data models for API requests and responses."""

from typing import Optional

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
