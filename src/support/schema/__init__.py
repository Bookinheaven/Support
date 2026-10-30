"""Schema modeling, parsing, and discovery adapters."""

from support.schema.models import (
    DatabaseSchema,
    TableSchema,
    ColumnSchema,
    ForeignKeySchema,
    UniqueConstraintSchema,
)
from support.schema.base import (
    BaseSchemaSource,
    SchemaDiscoveryResult,
    DiscoveryWarning,
)
from support.schema.sql_parser import SQLDDLParser, detect_dialect_from_sql
from support.schema.merger import SchemaMerger
from support.schema.sources.sql_file import SQLFileSchemaSource
from support.schema.sources.sqlite_db import SQLiteSchemaSource
from support.schema.sources.live_db import LiveDatabaseSchemaSource
from support.schema.diagram import (
    generate_mermaid_er,
    save_diagram_file,
    get_default_diagram_path,
)

__all__ = [
    "DatabaseSchema",
    "TableSchema",
    "ColumnSchema",
    "ForeignKeySchema",
    "UniqueConstraintSchema",
    "BaseSchemaSource",
    "SchemaDiscoveryResult",
    "DiscoveryWarning",
    "SQLDDLParser",
    "detect_dialect_from_sql",
    "SchemaMerger",
    "SQLFileSchemaSource",
    "SQLiteSchemaSource",
    "LiveDatabaseSchemaSource",
    "generate_mermaid_er",
    "save_diagram_file",
    "get_default_diagram_path",
]
