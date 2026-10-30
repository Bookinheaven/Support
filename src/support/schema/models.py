"""Normalized database-independent schema representation models."""

from typing import Any, Optional
from pydantic import BaseModel, Field, ConfigDict

from support.core.dialect import Dialect


class ColumnSchema(BaseModel):
    """Normalized schema definition for a single column."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    name: str
    data_type: str = "UNKNOWN"
    nullable: Optional[bool] = None
    default_value: Optional[str] = None
    is_primary_key: bool = False
    is_unique: bool = False
    comment: Optional[str] = None
    source_origin: Optional[str] = None


class ForeignKeySchema(BaseModel):
    """Normalized foreign key relationship."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    name: Optional[str] = None
    constrained_columns: list[str] = Field(default_factory=list)
    referred_table: str
    referred_columns: list[str] = Field(default_factory=list)
    referred_schema: Optional[str] = None
    on_delete: Optional[str] = None
    on_update: Optional[str] = None

    @property
    def display_str(self) -> str:
        """Formatted representation of the FK link."""
        from_cols = ", ".join(self.constrained_columns)
        target_ref = f"{self.referred_schema}.{self.referred_table}" if self.referred_schema else self.referred_table
        to_cols = ", ".join(self.referred_columns)
        details = []
        if self.on_delete:
            details.append(f"ON DELETE {self.on_delete}")
        if self.on_update:
            details.append(f"ON UPDATE {self.on_update}")
        extra = f" ({', '.join(details)})" if details else ""
        return f"({from_cols}) -> {target_ref}({to_cols}){extra}"


class UniqueConstraintSchema(BaseModel):
    """Normalized table unique constraint."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    name: Optional[str] = None
    columns: list[str] = Field(default_factory=list)


class TableSchema(BaseModel):
    """Normalized schema representation for a database table."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    name: str
    schema_name: Optional[str] = None
    columns: list[ColumnSchema] = Field(default_factory=list)
    primary_key: list[str] = Field(default_factory=list)
    foreign_keys: list[ForeignKeySchema] = Field(default_factory=list)
    unique_constraints: list[UniqueConstraintSchema] = Field(default_factory=list)
    source_origin: Optional[str] = None
    source_origins: list[str] = Field(default_factory=list)
    raw_ddl: Optional[str] = None

    @property
    def full_name(self) -> str:
        """Full qualified table identifier, including schema if available."""
        if self.schema_name:
            return f"{self.schema_name}.{self.name}"
        return self.name

    @property
    def display_name(self) -> str:
        """Formatted display name for UI presentation."""
        return self.full_name

    def get_column(self, col_name: str) -> Optional[ColumnSchema]:
        """Case-insensitive column lookup."""
        target = col_name.strip().lower()
        for col in self.columns:
            if col.name.strip().lower() == target:
                return col
        return None

    def add_or_update_column(self, column: ColumnSchema) -> None:
        """Add a column or update an existing one."""
        existing = self.get_column(column.name)
        if existing:
            # Update fields if new column has more information
            if column.data_type and column.data_type != "UNKNOWN":
                existing.data_type = column.data_type
            if column.nullable is not None:
                existing.nullable = column.nullable
            if column.default_value is not None:
                existing.default_value = column.default_value
            if column.is_primary_key:
                existing.is_primary_key = True
            if column.is_unique:
                existing.is_unique = True
            if column.comment:
                existing.comment = column.comment
        else:
            self.columns.append(column)


class DatabaseSchema(BaseModel):
    """Normalized schema representation for an entire database or project."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    dialect: Dialect = Dialect.UNKNOWN
    tables: dict[str, TableSchema] = Field(default_factory=dict)
    source_origins: list[str] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)

    @property
    def table_names(self) -> list[str]:
        """Sorted list of table names."""
        return sorted(self.tables.keys())

    @property
    def total_columns(self) -> int:
        """Total count of columns across all tables."""
        return sum(len(t.columns) for t in self.tables.values())

    @property
    def total_foreign_keys(self) -> int:
        """Total count of foreign keys across all tables."""
        return sum(len(t.foreign_keys) for t in self.tables.values())

    def get_table(
        self,
        name: str,
        schema_name: Optional[str] = None,
        allow_stem_match: bool = False,
    ) -> Optional[TableSchema]:
        """Lookup table by name and optional schema name."""
        if not schema_name and "." in name:
            parts = name.split(".", 1)
            schema_name = parts[0]
            name = parts[1]

        full_name = f"{schema_name}.{name}" if schema_name else name
        if full_name in self.tables:
            return self.tables[full_name]

        # Case-insensitive full_name lookup
        full_lower = full_name.lower()
        for key, table in self.tables.items():
            if key.lower() == full_lower or table.full_name.lower() == full_lower:
                return table

        # Fallback to case-insensitive name search
        target_name = name.lower()
        candidates: list[TableSchema] = []
        for key, table in self.tables.items():
            if table.name.lower() == target_name:
                if schema_name is None:
                    candidates.append(table)
                elif table.schema_name and table.schema_name.lower() == schema_name.lower():
                    return table

        if len(candidates) == 1:
            return candidates[0]
        elif len(candidates) > 1:
            # If multiple exist across schemas and schema_name was not specified,
            # prioritize standard default schemas (e.g., 'public' for PG, 'dbo' for MSSQL)
            for c in candidates:
                if c.schema_name and c.schema_name.lower() in ("public", "dbo"):
                    return c
            return candidates[0]

        if not allow_stem_match:
            return None

        # Fallback to singular / plural stem matching if unambiguous
        stem_candidates: list[TableSchema] = []
        for key, table in self.tables.items():
            t_low = table.name.lower()
            if t_low.endswith("s") and t_low[:-1] == target_name:
                stem_candidates.append(table)
            elif t_low.endswith("ies") and t_low[:-3] + "y" == target_name:
                stem_candidates.append(table)
            elif target_name.endswith("s") and target_name[:-1] == t_low:
                stem_candidates.append(table)
            elif target_name.endswith("ies") and target_name[:-3] + "y" == t_low:
                stem_candidates.append(table)

        if len(stem_candidates) == 1:
            return stem_candidates[0]
        elif len(stem_candidates) > 1 and schema_name:
            filtered = [t for t in stem_candidates if t.schema_name and t.schema_name.lower() == schema_name.lower()]
            if len(filtered) == 1:
                return filtered[0]

        return None

    def add_table(self, table: TableSchema) -> None:
        """Add or register a table."""
        self.tables[table.full_name] = table
        for origin in table.source_origins:
            if origin not in self.source_origins:
                self.source_origins.append(origin)
        if table.source_origin and table.source_origin not in self.source_origins:
            self.source_origins.append(table.source_origin)
