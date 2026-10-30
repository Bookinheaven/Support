"""Natural language query generation, validation, and controlled execution subsystem."""

from support.query.executor import ExecutionConfig, QueryResult, ReadOnlyQueryExecutor
from support.query.generator import QueryGenerator
from support.query.models import QueryDraft
from support.query.validator import validate_sql_query

from support.query.repair import (
    build_repair_prompt,
    detect_intended_operation,
    repair_sql_statement,
)

__all__ = [
    "ExecutionConfig",
    "QueryDraft",
    "QueryGenerator",
    "QueryResult",
    "ReadOnlyQueryExecutor",
    "build_repair_prompt",
    "detect_intended_operation",
    "repair_sql_statement",
    "validate_sql_query",
]
