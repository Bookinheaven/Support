"""Domain models for normalized database schema metadata."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any


class ReadOnlyDict(dict):
    """An immutable dictionary subclass preserving native dict behavior and JSON serializability."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        temp = dict(*args, **kwargs)
        for k, v in list(temp.items()):
            if isinstance(v, dict) and not isinstance(v, ReadOnlyDict):
                temp[k] = ReadOnlyDict(v)
            elif isinstance(v, (list, tuple)):
                temp[k] = tuple(ReadOnlyDict(item) if isinstance(item, dict) else item for item in v)
        super().__init__(temp)

    def __setitem__(self, key: Any, value: Any) -> None:
        raise TypeError("ReadOnlyDict is immutable.")

    def __delitem__(self, key: Any) -> None:
        raise TypeError("ReadOnlyDict is immutable.")

    def clear(self) -> None:
        raise TypeError("ReadOnlyDict is immutable.")

    def update(self, *args: Any, **kwargs: Any) -> None:
        raise TypeError("ReadOnlyDict is immutable.")

    def pop(self, *args: Any, **kwargs: Any) -> Any:
        raise TypeError("ReadOnlyDict is immutable.")

    def popitem(self) -> tuple[Any, Any]:
        raise TypeError("ReadOnlyDict is immutable.")

    def setdefault(self, *args: Any, **kwargs: Any) -> Any:
        raise TypeError("ReadOnlyDict is immutable.")


def unquote_identifier(ident: str) -> str:
    """Strip matching quotes (double quotes, square brackets, backticks) from an identifier."""
    ident = ident.strip()
    if len(ident) >= 2:
        if (ident.startswith('"') and ident.endswith('"')) or (ident.startswith('`') and ident.endswith('`')):
            return ident[1:-1]
        if ident.startswith('[') and ident.endswith(']'):
            return ident[1:-1]
    return ident


def parse_object_name(name: str) -> tuple[str | None, str]:
    """Parse object name into optional (schema, table).

    Handles:
    - "schema"."table"
    - [schema].[table]
    - `schema`.`table`
    - schema.table
    - table
    """
    raw = name.strip()
    if not raw:
        return None, ""

    if raw.startswith(('"', '[', '`')):
        close_char = ']' if raw[0] == '[' else raw[0]
        close_idx = raw.find(close_char, 1)
        if close_idx != -1 and close_idx + 1 < len(raw) and raw[close_idx + 1] == '.':
            schema_part = unquote_identifier(raw[:close_idx + 1])
            table_part = unquote_identifier(raw[close_idx + 2:])
            return schema_part, table_part
        if close_idx == len(raw) - 1:
            return None, unquote_identifier(raw)

    if '.' in raw:
        schema_part, table_part = raw.split('.', 1)
        return unquote_identifier(schema_part), unquote_identifier(table_part)

    return None, unquote_identifier(raw)


class TableType(StrEnum):
    """Type of database object."""

    TABLE = "table"
    VIEW = "view"
    COLLECTION = "collection"


@dataclass(frozen=True)
class ColumnMetadata:
    """Metadata for a relational database column."""

    name: str
    data_type: str
    nullable: bool = True
    default: str | None = None
    ordinal_position: int = 1


@dataclass(frozen=True)
class ConstraintMetadata:
    """Constraint definition (primary key, unique, check)."""

    name: str | None
    columns: tuple[str, ...]
    constraint_type: str  # "PRIMARY KEY", "UNIQUE", "CHECK"
    check_clause: str | None = None


@dataclass(frozen=True)
class ForeignKeyMetadata:
    """Foreign key relationship between tables."""

    name: str | None
    source_table: str
    source_columns: tuple[str, ...]
    target_table: str
    target_columns: tuple[str, ...]
    source_schema: str | None = None
    target_schema: str | None = None
    on_delete: str | None = None
    on_update: str | None = None

    @property
    def source_display(self) -> str:
        """Formatted source table and columns."""
        src_tbl = f"{self.source_schema}.{self.source_table}" if self.source_schema else self.source_table
        if len(self.source_columns) == 1:
            return f"{src_tbl}.{self.source_columns[0]}"
        return f"{src_tbl}.({', '.join(self.source_columns)})"

    @property
    def target_display(self) -> str:
        """Formatted target table and columns."""
        tgt_tbl = f"{self.target_schema}.{self.target_table}" if self.target_schema else self.target_table
        if len(self.target_columns) == 1:
            return f"{tgt_tbl}.{self.target_columns[0]}"
        return f"{tgt_tbl}.({', '.join(self.target_columns)})"


@dataclass(frozen=True)
class IndexMetadata:
    """Index metadata for a table or collection."""

    name: str
    columns: tuple[str, ...]
    unique: bool = False
    is_primary: bool = False
    method: str | None = None  # e.g. "BTREE", "HASH", None
    column_orders: tuple[str, ...] = ()  # Key order per column e.g. ("ASC", "DESC") or ("1", "-1", "2dsphere")
    included_columns: tuple[str, ...] = ()  # Covered non-key columns
    filter_condition: str | None = None  # Partial index predicate


@dataclass(frozen=True)
class TableMetadata:
    """Metadata for a single table, view, or collection."""

    name: str
    table_type: TableType = TableType.TABLE
    schema_name: str | None = None
    columns: tuple[ColumnMetadata, ...] = ()
    primary_key: ConstraintMetadata | None = None
    unique_constraints: tuple[ConstraintMetadata, ...] = ()
    foreign_keys: tuple[ForeignKeyMetadata, ...] = ()
    check_constraints: tuple[ConstraintMetadata, ...] = ()
    indexes: tuple[IndexMetadata, ...] = ()
    options: dict[str, Any] = field(default_factory=dict)
    validator: dict[str, Any] | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.options, ReadOnlyDict):
            object.__setattr__(self, "options", ReadOnlyDict(self.options or {}))
        if self.validator is not None and not isinstance(self.validator, ReadOnlyDict):
            object.__setattr__(self, "validator", ReadOnlyDict(self.validator))

    @property
    def qualified_name(self) -> str:
        """Return schema-qualified name if schema is present, else plain name."""
        if self.schema_name:
            return f"{self.schema_name}.{self.name}"
        return self.name


@dataclass(frozen=True)
class DatabaseSchema:
    """Comprehensive normalized database schema metadata."""

    database_name: str
    engine: str
    schemas: tuple[str, ...] = ()
    tables: tuple[TableMetadata, ...] = ()
    relationships: tuple[ForeignKeyMetadata, ...] = ()

    def get_table(self, name: str, schema: str | None = None) -> TableMetadata | None:
        """Resolve a table by name and optional schema."""
        raw_name = name.strip()
        if not raw_name:
            return None

        clean_schema = unquote_identifier(schema) if schema else None
        clean_name = unquote_identifier(raw_name)

        # 1. Check if table matches clean_name directly without dot-splitting
        direct_matches = [
            t
            for t in self.tables
            if (clean_schema is None or (t.schema_name and (t.schema_name == clean_schema or t.schema_name.lower() == clean_schema.lower())))
            and (t.name == clean_name or t.name.lower() == clean_name.lower())
        ]

        # 2. If clean_schema was None and we found no match and there is a dot in raw_name,
        # try parsing schema.table:
        if not direct_matches and clean_schema is None and "." in raw_name:
            parsed_schema, parsed_name = parse_object_name(raw_name)
            if parsed_schema is not None:
                return self.get_table(parsed_name, schema=parsed_schema)

        if not direct_matches:
            return None

        # 3. Disambiguation:
        # First: check for exact case-sensitive match
        exact_matches = [
            t
            for t in direct_matches
            if t.name == clean_name
            and (clean_schema is None or t.schema_name == clean_schema)
        ]
        if len(exact_matches) == 1:
            return exact_matches[0]
        elif len(exact_matches) > 1:
            from support.application.errors import AmbiguousObjectNameError
            schemas_list = [t.schema_name or "(default)" for t in exact_matches]
            raise AmbiguousObjectNameError(
                f"Object name '{name}' is ambiguous across schemas ({', '.join(schemas_list)}). "
                "Specify schema as <schema>.<name>."
            )

        # Second: case-insensitive match (when no single exact match exists)
        if len(direct_matches) == 1:
            return direct_matches[0]
        else:
            from support.application.errors import AmbiguousObjectNameError
            schemas_list = [t.schema_name or "(default)" for t in direct_matches]
            raise AmbiguousObjectNameError(
                f"Object name '{name}' is ambiguous across schemas ({', '.join(schemas_list)}). "
                "Specify schema as <schema>.<name>."
            )

