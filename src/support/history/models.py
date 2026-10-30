"""Models for query history entries and metadata."""

from datetime import datetime, timezone
from typing import Optional
from pydantic import BaseModel, Field


class QueryHistoryEntry(BaseModel):
    """Represents a historical query interaction in Support.

    Stores question, draft SQL, explanation, and validation/execution outcome.
    Query result rows and database credentials are NEVER persisted in history.
    """

    id: int
    timestamp: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    question: str
    sql: str
    explanation: Optional[str] = None
    database: str
    dialect: str
    is_validated: bool = False
    validation_status: str = "Not validated"
    is_schema_verified: Optional[bool] = None
    schema_status: Optional[str] = None
    execution_status: Optional[str] = None
    duration_ms: Optional[float] = None
    row_count: Optional[int] = None
    error: Optional[str] = None
    entry_type: str = "query"
