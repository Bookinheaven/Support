"""Data models for natural-language query generation drafts."""

from dataclasses import dataclass, field
from typing import Optional


@dataclass
class QueryDraft:
    """Internal typed representation of an AI-generated SQL query draft."""

    question: str
    sql: str
    explanation: str
    dialect: str
    validation_status: str
    is_validated: bool
    is_parsed: bool = True
    schema_status: str = "Not checked"
    is_schema_verified: Optional[bool] = None
    execution_status: str = "Not performed"
    operation: str = "SELECT"
    is_readonly: bool = True
    provider: str = ""
    model: str = ""
    clarification: Optional[str] = None
    tables_used: list[str] = field(default_factory=list)
    unknown_tables: list[str] = field(default_factory=list)
    unknown_columns: list[str] = field(default_factory=list)
    schema_coverage: str = ""
    raw_response: Optional[str] = None
    correction_notes: Optional[str] = None
    repair_attempts: int = 0
    post_exec_repaired: bool = False
    mutation_scope: str = "READ_ONLY"
    mutation_limit: Optional[int] = None
    is_bounded: bool = False
    is_deterministic: bool = False
    determinism_reason: Optional[str] = None
    is_unrestricted: bool = False
    selection_criteria: Optional[str] = None
    target_table: Optional[str] = None
    is_ordered: bool = False

    @property
    def is_clarification(self) -> bool:
        """True if the draft represents a clarification question rather than an executable SQL statement."""
        return bool(self.clarification and self.clarification.strip() and not self.sql)
