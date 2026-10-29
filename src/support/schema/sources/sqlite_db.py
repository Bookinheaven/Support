"""Schema source adapter for inspecting SQLite database files in read-only mode."""

import sqlite3
from collections import defaultdict
from pathlib import Path
from typing import Any
from support.core.dialect import Dialect
from support.core.exceptions import InvalidPathError, DatabaseAccessError
from support.schema.base import BaseSchemaSource, SchemaDiscoveryResult, DiscoveryWarning
from support.schema.models import (
    DatabaseSchema,
    TableSchema,
    ColumnSchema,
    ForeignKeySchema,
    UniqueConstraintSchema,
)


class SQLiteSchemaSource(BaseSchemaSource):
    """Inspects an existing SQLite database file in read-only mode."""

    def __init__(self, db_path: Path | str) -> None:
        self.db_path = Path(db_path).resolve()

    def discover(self) -> SchemaDiscoveryResult:
        """Inspect the SQLite database and return normalized schema metadata."""
        if not self.db_path.exists():
            raise InvalidPathError(self.db_path, "SQLite database file does not exist")
        if not self.db_path.is_file():
            raise InvalidPathError(self.db_path, "Target SQLite path is not a file")

        # Verify SQLite header magic bytes before opening
        try:
            with open(self.db_path, "rb") as f:
                header = f.read(16)
                if not header.startswith(b"SQLite format 3\x00"):
                    raise DatabaseAccessError(
                        self.db_path, "File is not a valid SQLite 3 database"
                    )
        except Exception as e:
            if isinstance(e, DatabaseAccessError):
                raise
            raise DatabaseAccessError(self.db_path, f"Failed to read file header: {e}")

        # Open in strictly read-only mode using URI
        uri = f"file:{self.db_path.as_posix()}?mode=ro"
        try:
            conn = sqlite3.connect(uri, uri=True)
            cursor = conn.cursor()
        except Exception as e:
            raise DatabaseAccessError(self.db_path, f"Failed to connect in read-only mode: {e}")

        schema = DatabaseSchema(dialect=Dialect.SQLITE)
        origin_str = str(self.db_path)
        schema.source_origins.append(origin_str)
        warnings: list[DiscoveryWarning] = []

        try:
            # Query tables from sqlite_master
            cursor.execute(
                "SELECT name, sql FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name;"
            )
            tables = cursor.fetchall()

            for table_name, raw_sql in tables:
                table_schema = self._inspect_table(cursor, table_name, raw_sql, origin_str, warnings)
                schema.add_table(table_schema)

        except Exception as e:
            raise DatabaseAccessError(self.db_path, f"Failed querying schema metadata: {e}")
        finally:
            conn.close()

        return SchemaDiscoveryResult(
            schema=schema,
            discovered_files=[self.db_path],
            parsed_files=[self.db_path],
            failed_files={},
            warnings=warnings,
        )

    def _inspect_table(
        self,
        cursor: sqlite3.Cursor,
        table_name: str,
        raw_sql: str | None,
        origin_str: str,
        warnings: list[DiscoveryWarning],
    ) -> TableSchema:
        """Inspect columns, primary keys, foreign keys, and indexes for a single SQLite table."""
        table = TableSchema(
            name=table_name,
            source_origin=origin_str,
            source_origins=[origin_str],
            raw_ddl=raw_sql,
        )

        # 1. Inspect columns: PRAGMA table_info
        # Returns: (cid, name, type, notnull, dflt_value, pk)
        cursor.execute(f'PRAGMA table_info("{table_name}");')
        col_rows = cursor.fetchall()

        # Sort PK columns by pk position if composite
        pk_entries: list[tuple[int, str]] = []

        for cid, col_name, col_type, notnull, dflt_value, pk in col_rows:
            is_pk = bool(pk > 0)
            if is_pk:
                pk_entries.append((pk, col_name))

            nullable = not bool(notnull)
            col_schema = ColumnSchema(
                name=col_name,
                data_type=col_type if col_type else "TEXT",
                nullable=nullable,
                default_value=str(dflt_value) if dflt_value is not None else None,
                is_primary_key=is_pk,
                source_origin=origin_str,
            )
            table.columns.append(col_schema)

        # Reconstruct ordered primary key
        pk_entries.sort(key=lambda x: x[0])
        table.primary_key = [name for _, name in pk_entries]

        # 2. Inspect foreign keys: PRAGMA foreign_key_list
        # Returns: (id, seq, table, from, to, on_update, on_delete, match)
        cursor.execute(f'PRAGMA foreign_key_list("{table_name}");')
        fk_rows = cursor.fetchall()

        fks_by_id: dict[int, list[Any]] = defaultdict(list)
        for row in fk_rows:
            fks_by_id[row[0]].append(row)

        for fk_id, rows in fks_by_id.items():
            rows.sort(key=lambda r: r[1])  # sort by seq
            target_table = rows[0][2]
            source_cols = [r[3] for r in rows]
            target_cols = [r[4] for r in rows if r[4] is not None]
            on_update = rows[0][5] if rows[0][5] not in ("NO ACTION", "NONE") else None
            on_delete = rows[0][6] if rows[0][6] not in ("NO ACTION", "NONE") else None

            table.foreign_keys.append(
                ForeignKeySchema(
                    name=f"fk_{table_name}_{fk_id}",
                    constrained_columns=source_cols,
                    referred_table=target_table,
                    referred_columns=target_cols,
                    on_delete=on_delete,
                    on_update=on_update,
                )
            )

        # 3. Inspect unique constraints / indexes: PRAGMA index_list
        # Returns: (seq, name, unique, origin, partial)
        cursor.execute(f'PRAGMA index_list("{table_name}");')
        index_rows = cursor.fetchall()

        for seq, index_name, is_unique, origin, partial in index_rows:
            if is_unique:
                cursor.execute(f'PRAGMA index_info("{index_name}");')
                idx_cols = [r[2] for r in sorted(cursor.fetchall(), key=lambda x: x[0])]
                if idx_cols:
                    # Do not duplicate if it is the primary key index
                    if set(idx_cols) == set(table.primary_key):
                        continue
                    table.unique_constraints.append(
                        UniqueConstraintSchema(name=index_name, columns=idx_cols)
                    )
                    if len(idx_cols) == 1:
                        col = table.get_column(idx_cols[0])
                        if col:
                            col.is_unique = True

        return table
