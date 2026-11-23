"""Terminal presentation, styling, and status formatting utilities using Rich."""

from __future__ import annotations

import os
import sys
import time
import urllib.parse
from dataclasses import dataclass
from typing import TYPE_CHECKING

from rich.console import Console

from support.domain.profiles import (
    ConnectionConfig,
    DatabaseType,
    MariaDBConnection,
    MongoDBConnection,
    MySQLConnection,
    PostgresConnection,
    SQLiteConnection,
    SQLServerConnection,
)
from support.domain.schema import DatabaseSchema, TableMetadata, TableType

if TYPE_CHECKING:
    from support.application.ports import ConnectionTestResult

ENGINE_DISPLAY_NAMES = {
    "postgresql": "PostgreSQL",
    "mysql": "MySQL",
    "mariadb": "MariaDB",
    "sqlite": "SQLite",
    "sqlserver": "SQL Server",
    "mongodb": "MongoDB",
}


def get_console() -> Console:
    """Return a Rich console respecting NO_COLOR and terminal capabilities."""
    no_color = "NO_COLOR" in os.environ
    return Console(no_color=no_color, highlight=False)


def format_engine_name(db_type: str | DatabaseType | None) -> str:
    """Return human-readable database engine name."""
    if not db_type:
        return "Unknown"
    val = db_type.value if hasattr(db_type, "value") else str(db_type)
    return ENGINE_DISPLAY_NAMES.get(val.strip().lower(), val.capitalize())


def format_duration(seconds: float | None) -> str:
    """Format connection-test duration into readable milliseconds or seconds."""
    if seconds is None:
        return ""
    ms = seconds * 1000.0
    if ms < 1.0:
        return "< 1 ms"
    elif ms >= 1000.0:
        return f"{seconds:.2f} s"
    return f"{round(ms)} ms"


def format_endpoint(connection: ConnectionConfig | None) -> str:
    """Safely format database endpoint without exposing sensitive credentials."""
    if connection is None:
        return "none"
    if isinstance(connection, SQLiteConnection):
        return connection.database_path
    elif isinstance(connection, MongoDBConnection):
        if connection.uri:
            try:
                parsed = urllib.parse.urlsplit(connection.uri)
                host = parsed.hostname or "localhost"
                port = f":{parsed.port}" if parsed.port else ""
                return f"{host}{port}"
            except Exception:
                return connection.host or "localhost:27017"
        host = connection.host or "localhost"
        port = f":{connection.port}" if connection.port else ""
        return f"{host}{port}"
    elif isinstance(connection, (PostgresConnection, MySQLConnection, MariaDBConnection, SQLServerConnection)):
        return f"{connection.host}:{connection.port}"
    return "unknown"


@dataclass
class DatabaseStatusInfo:
    """Snapshot of database connectivity status for the active session."""

    profile_name: str | None = None
    engine_name: str | None = None
    database_name: str | None = None
    endpoint: str | None = None
    server_version: str | None = None
    is_online: bool = False
    duration_seconds: float | None = None
    failure_reason: str | None = None


def render_database_status_line(info: DatabaseStatusInfo) -> str:
    """Render compact, color-coded database status line."""
    if not info.profile_name or info.profile_name == "(none)":
        return "[cyan]Database:[/cyan]  (none)  [dim]·[/dim]  [yellow]Not configured[/yellow]"

    engine = info.engine_name or "Database"
    target = f"{info.profile_name} ({engine})"

    if info.is_online:
        duration_str = ""
        if info.duration_seconds is not None:
            formatted_time = format_duration(info.duration_seconds)
            duration_str = f"  [dim]·[/dim]  [green]{formatted_time}[/green]"
        return f"[cyan]Database:[/cyan]  {target}  [dim]·[/dim]  [green]Online[/green]{duration_str}"
    else:
        line = f"[cyan]Database:[/cyan]  {target}  [dim]·[/dim]  [red]Offline[/red]"
        if info.failure_reason:
            line += f"\n           [dim]Reason: {info.failure_reason}[/dim]"
        return line


def render_model_status_line(
    model_name: str | None = None,
    is_available: bool = False,
    details: str | None = None,
) -> str:
    """Render compact, color-coded model status line."""
    if not model_name:
        return "[cyan]Model:[/cyan]     [yellow]Not configured[/yellow]"
    if is_available:
        return f"[cyan]Model:[/cyan]     {model_name}  [dim]·[/dim]  [green]Available[/green]"
    state_str = details or "Unavailable"
    return f"[cyan]Model:[/cyan]     {model_name}  [dim]·[/dim]  [red]{state_str}[/red]"


def print_status(
    console: Console,
    info: DatabaseStatusInfo,
    model_info: Any | None = None,
) -> None:
    """Display latest known database and model status without re-testing."""
    if not info.profile_name or info.profile_name == "(none)":
        console.print("No active database connection.\n")
        console.print("Use 'support db' to manage database connections.\n")
        return

    console.print(render_database_status_line(info))
    if model_info is not None:
        console.print(
            render_model_status_line(
                getattr(model_info, "name", None),
                getattr(model_info, "is_available", False),
                getattr(model_info, "details", None),
            )
        )
    else:
        console.print(render_model_status_line())

    details: list[tuple[str, str]] = []
    if info.database_name:
        details.append(("Database", info.database_name))
    if info.endpoint:
        details.append(("Endpoint", info.endpoint))
    if info.server_version:
        details.append(("Server", info.server_version))

    if details:
        console.print()
        for label, val in details:
            console.print(f"[dim]{label + ':':<11}[/dim]{val}")
    console.print()


def render_tables(
    console: Console,
    schema: DatabaseSchema,
    include_schema: bool = True,
    omit_schema: bool | None = None,
    numbered: bool = False,
    names_only: bool = False,
    alphabetical: bool = False,
) -> None:
    """Render list of tables, views, or collections with presentation preferences."""
    if omit_schema is not None:
        include_schema = not omit_schema

    if schema.engine.lower() == "mongodb":
        names = [t.name for t in schema.tables]
        if alphabetical:
            names = sorted(names, key=str.lower)
        if not names:
            console.print(f"No collections found in database '{schema.database_name}'.\n")
            return
        if not names_only:
            console.print(f"Collections in [cyan]{schema.database_name}[/cyan]:\n")
        for i, name in enumerate(names, 1):
            prefix = f"{i}. " if numbered else ""
            indent = "" if names_only else "  "
            console.print(f"{indent}{prefix}{name}")
        if not names_only:
            count = len(names)
            unit = "collection" if count == 1 else "collections"
            console.print(f"\n[dim]{count} {unit}[/dim]\n")
        else:
            console.print()
        return

    tables = [t for t in schema.tables if t.table_type == TableType.TABLE]
    views = [t for t in schema.tables if t.table_type == TableType.VIEW]

    def _get_display_name(table_meta: TableMetadata) -> str:
        if not include_schema:
            return table_meta.name
        return table_meta.qualified_name

    if alphabetical:
        tables = sorted(tables, key=lambda t: _get_display_name(t).lower())
        views = sorted(views, key=lambda t: _get_display_name(t).lower())

    if not tables and not views:
        console.print(f"No tables or views found in database '{schema.database_name}'.\n")
        return

    item_idx = 1
    if tables:
        if not names_only:
            console.print(f"Tables in [cyan]{schema.database_name}[/cyan]:\n")
        for t in tables:
            display_name = _get_display_name(t)
            prefix = f"{item_idx}. " if numbered else ""
            indent = "" if names_only else "  "
            console.print(f"{indent}{prefix}{display_name}")
            item_idx += 1

    if views:
        if not names_only:
            prefix_nl = "\n" if tables else ""
            console.print(f"{prefix_nl}Views in [cyan]{schema.database_name}[/cyan]:\n")
        for v in views:
            display_name = _get_display_name(v)
            prefix = f"{item_idx}. " if numbered else ""
            indent = "" if names_only else "  "
            console.print(f"{indent}{prefix}{display_name}")
            item_idx += 1

    if not names_only:
        summary_parts = []
        if tables:
            tbl_unit = "table" if len(tables) == 1 else "tables"
            summary_parts.append(f"{len(tables)} {tbl_unit}")
        if views:
            view_unit = "view" if len(views) == 1 else "views"
            summary_parts.append(f"{len(views)} {view_unit}")

        summary_str = " · ".join(summary_parts)
        console.print(f"\n[dim]{summary_str}[/dim]\n")
    else:
        console.print()


def render_schema_overview(console: Console, schema: DatabaseSchema) -> None:
    """Render high-level overview of database schema."""
    engine_name = format_engine_name(schema.engine)
    if schema.engine.lower() == "mongodb":
        col_count = len(schema.tables)
        total_indexes = sum(len(t.indexes) for t in schema.tables)
        console.print(f"[dim]Database:[/dim]    {schema.database_name}")
        console.print(f"[dim]Engine:[/dim]      {engine_name}")
        console.print(f"[dim]Collections:[/dim] {col_count}")
        console.print(f"[dim]Indexes:[/dim]     {total_indexes}\n")
        return

    tables = [t for t in schema.tables if t.table_type == TableType.TABLE]
    views = [t for t in schema.tables if t.table_type == TableType.VIEW]
    num_schemas = len(schema.schemas)
    num_relationships = len(schema.relationships)
    total_indexes = sum(len(t.indexes) for t in schema.tables)

    console.print(f"[dim]Database:[/dim]      {schema.database_name}")
    console.print(f"[dim]Engine:[/dim]        {engine_name}")
    if num_schemas > 1:
        console.print(f"[dim]Schemas:[/dim]       {num_schemas}")
    console.print(f"[dim]Tables:[/dim]        {len(tables)}")
    if views:
        console.print(f"[dim]Views:[/dim]         {len(views)}")
    console.print(f"[dim]Relationships:[/dim] {num_relationships}")
    console.print(f"[dim]Indexes:[/dim]       {total_indexes}\n")


def render_object_description(console: Console, table: TableMetadata, engine: str) -> None:
    """Render detailed structural description of a table, view, or collection."""
    engine_name = format_engine_name(engine)
    if engine.lower() == "mongodb" or table.table_type == TableType.COLLECTION:
        console.print(f"[cyan]Collection:[/cyan]  {table.name}")
        console.print(f"[dim]Engine:[/dim]      {engine_name}")
        if table.options:
            console.print("\n[bold]Options:[/bold]")
            for k, v in table.options.items():
                console.print(f"  [dim]{k}:[/dim] {v}")
        if table.validator:
            import json

            console.print("\n[bold]Validator:[/bold]")
            console.print("  " + json.dumps(table.validator, indent=2).replace("\n", "\n  "))
        if table.indexes:
            console.print("\n[bold]Indexes:[/bold]")
            for idx in table.indexes:
                attrs = []
                if idx.unique:
                    attrs.append("unique")
                if idx.method:
                    attrs.append(idx.method)
                attr_str = f" [dim]({', '.join(attrs)})[/dim]" if attrs else ""
                filter_str = f" [dim]Filter: {idx.filter_condition}[/dim]" if idx.filter_condition else ""
                console.print(f"  {idx.name}{attr_str}{filter_str}")
                for i, col in enumerate(idx.columns):
                    order = f" ({idx.column_orders[i]})" if i < len(idx.column_orders) else ""
                    console.print(f"    [dim]{col}{order}[/dim]")
        console.print()
        return

    type_display = "View" if table.table_type == TableType.VIEW else "Table"
    console.print(f"[cyan]{type_display}:[/cyan]       {table.qualified_name}")
    console.print(f"[dim]Engine:[/dim]      {engine_name}")

    if table.columns:
        console.print("\n[bold]Columns:[/bold]")
        max_name_len = max(len(c.name) for c in table.columns)
        max_type_len = max(len(c.data_type) for c in table.columns)
        pk_cols = set(table.primary_key.columns) if table.primary_key else set()

        for c in table.columns:
            null_str = "NULL    " if c.nullable else "NOT NULL"
            default_str = f"DEFAULT {c.default}" if c.default is not None else ""
            is_pk = "PK" if c.name in pk_cols else ""
            console.print(
                f"  {c.name:<{max_name_len}}  "
                f"[dim]{c.data_type:<{max_type_len}}[/dim]  "
                f"{null_str}  "
                f"[yellow]{is_pk:<2}[/yellow]  "
                f"[dim]{default_str}[/dim]"
            )

    if table.primary_key and table.primary_key.columns:
        pk_cols_str = ", ".join(table.primary_key.columns)
        pk_name = f" [dim]({table.primary_key.name})[/dim]" if table.primary_key.name else ""
        console.print(f"\n[bold]Primary Key:[/bold]{pk_name}\n  ({pk_cols_str})")

    if table.unique_constraints:
        console.print("\n[bold]Unique Constraints:[/bold]")
        for u in table.unique_constraints:
            u_name = f" [dim]({u.name})[/dim]" if u.name else ""
            console.print(f"  ({', '.join(u.columns)}){u_name}")

    if table.foreign_keys:
        console.print("\n[bold]Foreign Keys:[/bold]")
        for fk in table.foreign_keys:
            actions = []
            if fk.on_delete:
                actions.append(f"ON DELETE {fk.on_delete}")
            if fk.on_update:
                actions.append(f"ON UPDATE {fk.on_update}")
            act_str = f" [dim]{' '.join(actions)}[/dim]" if actions else ""
            console.print(f"  {fk.source_display} -> {fk.target_display}{act_str}")

    if table.check_constraints:
        console.print("\n[bold]Check Constraints:[/bold]")
        for chk in table.check_constraints:
            chk_name = f"{chk.name}: " if chk.name else ""
            console.print(f"  [dim]{chk_name}[/dim]{chk.check_clause}")

    if table.indexes:
        console.print("\n[bold]Indexes:[/bold]")
        for idx in table.indexes:
            attrs = []
            if idx.unique:
                attrs.append("unique")
            if idx.method:
                attrs.append(idx.method)
            attr_str = f" [dim]({', '.join(attrs)})[/dim]" if attrs else ""
            cols_display = []
            for i, c in enumerate(idx.columns):
                if i < len(idx.column_orders) and idx.column_orders[i] == "DESC":
                    cols_display.append(f"{c} DESC")
                else:
                    cols_display.append(c)
            cols_str = ", ".join(cols_display)
            inc_str = f" INCLUDE ({', '.join(idx.included_columns)})" if idx.included_columns else ""
            where_str = f" WHERE {idx.filter_condition}" if idx.filter_condition else ""
            console.print(f"  {idx.name}{attr_str}\n    ({cols_str}){inc_str}{where_str}")

    console.print()


def render_relationships(
    console: Console,
    schema: DatabaseSchema,
    names_only: bool = False,
) -> None:
    """Render foreign-key relationships across the database."""
    if schema.engine.lower() == "mongodb":
        console.print("MongoDB collections do not define relational foreign-key constraints.\n")
        return

    relationships = schema.relationships
    if not relationships:
        console.print(f"No declared foreign key relationships found in database '{schema.database_name}'.\n")
        return

    if names_only:
        table_names = sorted({fk.source_table for fk in relationships})
        for name in table_names:
            console.print(f"  {name}")
        console.print()
        return

    console.print(f"Foreign-key relationships in [cyan]{schema.database_name}[/cyan]:\n")
    for fk in relationships:
        actions = []
        if fk.on_delete:
            actions.append(f"ON DELETE {fk.on_delete}")
        if fk.on_update:
            actions.append(f"ON UPDATE {fk.on_update}")
        act_str = f" [dim]({', '.join(actions)})[/dim]" if actions else ""
        console.print(f"  {fk.source_display}\n      -> {fk.target_display}{act_str}\n")

    count = len(relationships)
    unit = "relationship" if count == 1 else "relationships"
    console.print(f"[dim]{count} {unit}[/dim]\n")
