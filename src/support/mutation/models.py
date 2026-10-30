"""Data models for structured, schema-grounded mutation plans and execution results."""

from enum import Enum
from typing import Any, Optional
from pydantic import BaseModel, Field, ConfigDict


class MutationType(str, Enum):
    """Supported CRUD data modification operations."""

    INSERT = "INSERT"
    UPDATE = "UPDATE"
    DELETE = "DELETE"


class WhereCondition(BaseModel):
    """Structured condition clause for targeted record filtering."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    column: str
    operator: str = "="
    value: Any = None


class ChangePlan(BaseModel):
    """Schema-grounded, compiled, and verified mutation plan.

    Guarantees:
    - Independent schema validation (never arbitrary model SQL).
    - Parameterized execution binding (values separate from SQL text).
    - Cryptographic plan_hash linking approved target, statement, and parameters.
    - Identification of database defaults vs explicit user values vs inferred values.
    """

    model_config = ConfigDict(arbitrary_types_allowed=True)

    plan_id: str
    operation: MutationType
    database: str
    dialect: str
    table_name: str
    fields_to_set: dict[str, Any] = Field(default_factory=dict)
    inferred_fields: dict[str, str] = Field(default_factory=dict)
    default_fields: list[str] = Field(default_factory=list)
    conditions: list[WhereCondition] = Field(default_factory=list)
    preview_records: list[dict[str, Any]] = Field(default_factory=list)
    matched_record_count: int = 0
    sql: str = ""
    parameters: dict[str, Any] = Field(default_factory=dict)
    plan_hash: str = ""
    is_valid: bool = True
    validation_error: Optional[str] = None
    missing_required_fields: list[str] = Field(default_factory=list)
    foreign_key_warnings: list[str] = Field(default_factory=list)
    user_explanation: Optional[str] = None


class MutationResult(BaseModel):
    """Result of executing a verified mutation plan within an explicit transaction."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    is_success: bool
    status: str  # "COMMITTED", "ROLLED_BACK", "FAILED", "UNKNOWN"
    operation: str
    database: str
    table: str
    affected_rows: int = 0
    duration_ms: float = 0.0
    error_message: Optional[str] = None
    error_type: Optional[str] = None  # e.g., "UNIQUE_VIOLATION", "FOREIGN_KEY_VIOLATION", "TIMEOUT"
    inserted_id: Optional[Any] = None
    committed: bool = False
    rolled_back: bool = False


class PendingMutation(BaseModel):
    """Pending multi-turn mutation waiting for missing required field values."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    operation: MutationType
    table_name: str
    values: dict[str, Any] = Field(default_factory=dict)
    conditions: dict[str, Any] = Field(default_factory=dict)
    missing_fields: list[str] = Field(default_factory=list)
    allow_sample_data: bool = False
    question: str = ""
