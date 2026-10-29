"""Deterministic schema merging for multiple files and schema sources."""

from typing import Optional
from support.core.dialect import Dialect
from support.schema.models import DatabaseSchema, TableSchema
from support.schema.base import DiscoveryWarning


class SchemaMerger:
    """Merges multiple DatabaseSchema objects into a unified schema deterministically."""

    @staticmethod
    def merge(
        schemas: list[DatabaseSchema],
        forced_dialect: Optional[Dialect] = None,
    ) -> tuple[DatabaseSchema, list[DiscoveryWarning]]:
        """Merge a list of schemas and return the combined schema and warnings."""
        warnings: list[DiscoveryWarning] = []
        if not schemas:
            return DatabaseSchema(dialect=forced_dialect or Dialect.UNKNOWN), warnings

        # Determine dialect
        effective_dialect = forced_dialect
        if effective_dialect is None or effective_dialect == Dialect.UNKNOWN:
            detected_dialects = {s.dialect for s in schemas if s.dialect != Dialect.UNKNOWN}
            if len(detected_dialects) == 1:
                effective_dialect = detected_dialects.pop()
            elif len(detected_dialects) > 1:
                dialect_names = ", ".join(d.value for d in detected_dialects)
                warnings.append(
                    DiscoveryWarning(
                        message=f"Conflicting database dialects detected across files: {dialect_names}. Defaulting to unknown.",
                        severity="conflict",
                    )
                )
                effective_dialect = Dialect.UNKNOWN
            else:
                effective_dialect = Dialect.UNKNOWN

        merged_schema = DatabaseSchema(dialect=effective_dialect)

        for schema in schemas:
            # Aggregate origins
            for origin in schema.source_origins:
                if origin not in merged_schema.source_origins:
                    merged_schema.source_origins.append(origin)

            for table_key, table in schema.tables.items():
                existing_table = merged_schema.tables.get(table_key)
                if existing_table is None:
                    # Deep copy / clone table to prevent mutation leaks
                    new_table = table.model_copy(deep=True)
                    merged_schema.tables[table_key] = new_table
                else:
                    # Table already exists; apply deterministic merge
                    SchemaMerger._merge_tables(existing_table, table, warnings)

        return merged_schema, warnings

    @staticmethod
    def _merge_tables(
        dest: TableSchema,
        src: TableSchema,
        warnings: list[DiscoveryWarning],
    ) -> None:
        """Merge table src into table dest deterministically."""
        # Merge origins
        for origin in src.source_origins:
            if origin not in dest.source_origins:
                dest.source_origins.append(origin)
        if src.source_origin and src.source_origin not in dest.source_origins:
            dest.source_origins.append(src.source_origin)

        # Merge columns
        for src_col in src.columns:
            dest_col = dest.get_column(src_col.name)
            if dest_col is None:
                dest.columns.append(src_col.model_copy(deep=True))
            else:
                # Column exists: check for conflicting data types
                if (
                    src_col.data_type != "UNKNOWN"
                    and dest_col.data_type != "UNKNOWN"
                    and src_col.data_type.lower() != dest_col.data_type.lower()
                ):
                    warnings.append(
                        DiscoveryWarning(
                            message=(
                                f"Table '{dest.full_name}' column '{dest_col.name}' has conflicting data types: "
                                f"'{dest_col.data_type}' vs '{src_col.data_type}'. Retaining '{dest_col.data_type}'."
                            ),
                            file_path=src.source_origin,
                            severity="conflict",
                        )
                    )
                # Update missing metadata if available
                if dest_col.data_type == "UNKNOWN" and src_col.data_type != "UNKNOWN":
                    dest_col.data_type = src_col.data_type
                if dest_col.nullable is None and src_col.nullable is not None:
                    dest_col.nullable = src_col.nullable
                if dest_col.default_value is None and src_col.default_value is not None:
                    dest_col.default_value = src_col.default_value
                if src_col.is_primary_key:
                    dest_col.is_primary_key = True
                if src_col.is_unique:
                    dest_col.is_unique = True

        # Merge primary key
        if src.primary_key:
            if not dest.primary_key:
                dest.primary_key = list(src.primary_key)
            elif set(dest.primary_key) != set(src.primary_key):
                warnings.append(
                    DiscoveryWarning(
                        message=(
                            f"Table '{dest.full_name}' has conflicting PRIMARY KEY definitions: "
                            f"{dest.primary_key} vs {src.primary_key}."
                        ),
                        file_path=src.source_origin,
                        severity="conflict",
                    )
                )

        # Merge unique constraints (deduplicating by column list)
        existing_uqs = {tuple(sorted(uq.columns)) for uq in dest.unique_constraints}
        for uq in src.unique_constraints:
            col_tuple = tuple(sorted(uq.columns))
            if col_tuple not in existing_uqs:
                dest.unique_constraints.append(uq.model_copy(deep=True))
                existing_uqs.add(col_tuple)

        # Merge foreign keys (deduplicating by source columns + target table + target cols)
        existing_fks = {
            (
                tuple(sorted(fk.constrained_columns)),
                fk.referred_table.lower(),
                tuple(sorted(fk.referred_columns)),
            )
            for fk in dest.foreign_keys
        }
        for fk in src.foreign_keys:
            key = (
                tuple(sorted(fk.constrained_columns)),
                fk.referred_table.lower(),
                tuple(sorted(fk.referred_columns)),
            )
            if key not in existing_fks:
                dest.foreign_keys.append(fk.model_copy(deep=True))
                existing_fks.add(key)
