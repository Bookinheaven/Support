"""Live database schema inspection source using SQLAlchemy inspector."""

from typing import Any, Optional
from sqlalchemy import inspect, Engine
from sqlalchemy.exc import SQLAlchemyError

from support.core.dialect import Dialect
from support.core.exceptions import DatabaseAccessError
from support.db.models import ConnectionProfile
from support.db.connection import DatabaseConnectionFactory
from support.schema.base import BaseSchemaSource, SchemaDiscoveryResult, DiscoveryWarning
from support.schema.models import (
    DatabaseSchema,
    TableSchema,
    ColumnSchema,
    ForeignKeySchema,
    UniqueConstraintSchema,
)
from support.schema.sources.sqlite_db import SQLiteSchemaSource


class LiveDatabaseSchemaSource(BaseSchemaSource):
    """Discovers schema directly from a live database instance via SQLAlchemy."""

    def __init__(
        self,
        profile: ConnectionProfile,
        password: Optional[str] = None,
        engine: Optional[Engine] = None,
        target_schema: Optional[str] = None,
    ) -> None:
        self.profile = profile
        self.password = password
        self._engine = engine
        self.target_schema = target_schema or profile.options.get("schema")

    def discover(self) -> SchemaDiscoveryResult:
        """Inspect the live database and return normalized schema models."""
        # Special case: SQLite file-based database can reuse the dedicated read-only inspector
        # when no custom engine is supplied and path is not in-memory
        if self.profile.dialect == Dialect.SQLITE and self._engine is None and self.profile.database != ":memory:":
            sqlite_source = SQLiteSchemaSource(self.profile.database)
            return sqlite_source.discover()

        redacted_origin = self.profile.get_redacted_url()
        schema = DatabaseSchema(
            dialect=self.profile.dialect,
            source_origins=[f"live:{self.profile.name}"],
            metadata={
                "profile_name": self.profile.name,
                "target": self.profile.display_target,
                "redacted_url": redacted_origin,
            },
        )
        warnings: list[DiscoveryWarning] = []

        engine = self._engine
        should_dispose = False
        if engine is None:
            engine = DatabaseConnectionFactory.create_engine(self.profile, password=self.password)
            should_dispose = True

        try:
            inspector = inspect(engine)

            # Determine schemas to inspect
            schemas_to_scan: list[Optional[str]] = []
            if self.target_schema:
                schemas_to_scan.append(self.target_schema)
            elif self.profile.dialect == Dialect.SQLITE:
                schemas_to_scan = [None]
            elif self.profile.dialect == Dialect.MYSQL and self.profile.database:
                # In MySQL, database is the schema namespace; inspect target database
                schemas_to_scan = [self.profile.database]
            else:
                # System schemas to filter out
                system_schemas = {
                    "information_schema",
                    "pg_catalog",
                    "pg_toast",
                    "sys",
                    "guest",
                    "mysql",
                    "performance_schema",
                    "db_owner",
                    "db_accessadmin",
                    "db_securityadmin",
                    "db_ddladmin",
                    "db_backupoperator",
                    "db_datareader",
                    "db_datawriter",
                    "db_denydatareader",
                    "db_denydatawriter",
                }
                try:
                    all_schemas = inspector.get_schema_names()
                    user_schemas = [s for s in all_schemas if s and s.lower() not in system_schemas]
                    if user_schemas:
                        # Prioritize user schemas (public for PG, dbo for MSSQL)
                        user_schemas.sort(key=lambda s: (s.lower() != "public", s.lower() != "dbo", s.lower()))
                        schemas_to_scan = user_schemas
                    else:
                        schemas_to_scan = [None]
                except Exception:
                    schemas_to_scan = [None]

            for s_name in schemas_to_scan:
                try:
                    table_names = inspector.get_table_names(schema=s_name)
                except Exception as e:
                    warnings.append(
                        DiscoveryWarning(
                            message=f"Failed to query table names in schema '{s_name}': {e}",
                            file_path=redacted_origin,
                            severity="warning",
                        )
                    )
                    continue

                for table_name in table_names:
                    try:
                        table_schema = self._inspect_table(inspector, table_name, s_name, redacted_origin, warnings)
                        schema.add_table(table_schema)
                    except Exception as e:
                        warnings.append(
                            DiscoveryWarning(
                                message=f"Failed to inspect metadata for table '{table_name}': {e}",
                                file_path=redacted_origin,
                                severity="warning",
                            )
                        )

        except SQLAlchemyError as e:
            err_msg = str(e)
            if self.password and self.password in err_msg:
                err_msg = err_msg.replace(self.password, "***")

            low = err_msg.lower()
            if "permission denied" in low or "access denied" in low or "denied" in low:
                hint = "Insufficient permissions to inspect schema metadata. Ensure user has VIEW DEFINITION / SELECT permissions on system catalogs."
            elif "timed out" in low or "timeout" in low:
                hint = "Connection timed out during schema inspection. Check network connectivity and server status."
            elif "connection refused" in low or "not reachable" in low or "failed to connect" in low:
                hint = "Database server is unreachable. Check that host and port are online and accept connections."
            else:
                hint = "Ensure database driver is compatible and credentials are valid."

            raise DatabaseAccessError(redacted_origin, f"Live inspection failure: {err_msg}", details=hint)
        finally:
            if should_dispose and engine is not None:
                engine.dispose()

        return SchemaDiscoveryResult(
            schema=schema,
            discovered_files=[],
            parsed_files=[],
            failed_files={},
            warnings=warnings,
        )

    def _inspect_table(
        self,
        inspector: Any,
        table_name: str,
        schema_name: Optional[str],
        origin_str: str,
        warnings: list[DiscoveryWarning],
    ) -> TableSchema:
        """Inspect columns, primary key, foreign keys, and unique constraints for a table."""
        table = TableSchema(
            name=table_name,
            schema_name=schema_name,
            source_origin=origin_str,
            source_origins=[origin_str],
        )

        # 1. Columns
        try:
            columns_meta = inspector.get_columns(table_name, schema=schema_name)
            for col in columns_meta:
                col_name = col["name"]
                data_type_str = str(col.get("type", "UNKNOWN"))
                nullable = col.get("nullable", True)
                dflt = col.get("default")
                dflt_str = str(dflt) if dflt is not None else None
                is_pk = bool(col.get("primary_key", 0))

                col_schema = ColumnSchema(
                    name=col_name,
                    data_type=data_type_str,
                    nullable=nullable,
                    default_value=dflt_str,
                    is_primary_key=is_pk,
                    comment=col.get("comment"),
                    source_origin=origin_str,
                )
                table.columns.append(col_schema)
        except Exception as e:
            warnings.append(
                DiscoveryWarning(
                    message=f"Could not read columns for '{table_name}': {e}",
                    file_path=origin_str,
                    severity="warning",
                )
            )

        # 2. Primary Key
        try:
            pk_constraint = inspector.get_pk_constraint(table_name, schema=schema_name)
            if pk_constraint and pk_constraint.get("constrained_columns"):
                table.primary_key = list(pk_constraint["constrained_columns"])
                for pk_col_name in table.primary_key:
                    c = table.get_column(pk_col_name)
                    if c:
                        c.is_primary_key = True
        except Exception as e:
            warnings.append(
                DiscoveryWarning(
                    message=f"Could not read primary key for '{table_name}': {e}",
                    file_path=origin_str,
                    severity="warning",
                )
            )

        # Fallback if primary_key is empty: collect columns flagged is_primary_key
        if not table.primary_key:
            table.primary_key = [c.name for c in table.columns if c.is_primary_key]

        # 3. Foreign Keys
        try:
            fks_meta = inspector.get_foreign_keys(table_name, schema=schema_name)
            for fk in fks_meta:
                fk_name = fk.get("name")
                constrained = fk.get("constrained_columns", [])
                ref_table = fk.get("referred_table", "")
                ref_schema = fk.get("referred_schema")
                ref_cols = fk.get("referred_columns", [])
                options = fk.get("options", {})
                on_delete = options.get("ondelete")
                on_update = options.get("onupdate")

                table.foreign_keys.append(
                    ForeignKeySchema(
                        name=fk_name,
                        constrained_columns=list(constrained),
                        referred_table=ref_table,
                        referred_columns=list(ref_cols),
                        referred_schema=ref_schema,
                        on_delete=str(on_delete) if on_delete else None,
                        on_update=str(on_update) if on_update else None,
                    )
                )
        except Exception as e:
            warnings.append(
                DiscoveryWarning(
                    message=f"Could not read foreign keys for '{table_name}': {e}",
                    file_path=origin_str,
                    severity="warning",
                )
            )

        # 4. Unique Constraints
        seen_uq_cols: set[tuple[str, ...]] = set()
        try:
            uqs_meta = inspector.get_unique_constraints(table_name, schema=schema_name)
            for uq in uqs_meta:
                col_names = [c for c in uq.get("column_names", []) if c]
                if col_names:
                    col_tuple = tuple(col_names)
                    if set(col_names) == set(table.primary_key):
                        continue
                    if col_tuple in seen_uq_cols:
                        continue
                    seen_uq_cols.add(col_tuple)
                    table.unique_constraints.append(
                        UniqueConstraintSchema(name=uq.get("name"), columns=list(col_names))
                    )
                    if len(col_names) == 1:
                        c = table.get_column(col_names[0])
                        if c:
                            c.is_unique = True
        except (NotImplementedError, Exception) as e:
            if not isinstance(e, NotImplementedError):
                warnings.append(
                    DiscoveryWarning(
                        message=f"Could not read unique constraints for '{table_name}': {e}",
                        file_path=origin_str,
                        severity="warning",
                    )
                )

        # Augment with unique indexes (crucial for MSSQL, MySQL, and SQLite)
        try:
            indexes_meta = inspector.get_indexes(table_name, schema=schema_name)
            for idx in indexes_meta:
                if idx.get("unique"):
                    col_names = [c for c in idx.get("column_names", []) if c]
                    if col_names:
                        col_tuple = tuple(col_names)
                        if set(col_names) == set(table.primary_key):
                            continue
                        if col_tuple in seen_uq_cols:
                            continue
                        seen_uq_cols.add(col_tuple)
                        table.unique_constraints.append(
                            UniqueConstraintSchema(name=idx.get("name"), columns=list(col_names))
                        )
                        if len(col_names) == 1:
                            c = table.get_column(col_names[0])
                            if c:
                                c.is_unique = True
        except Exception:
            pass

        return table
