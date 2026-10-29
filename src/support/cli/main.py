"""Command-line interface entrypoint for Support."""

import sys
import json
from pathlib import Path
from typing import Optional

# Ensure UTF-8 output streams across environments (Windows cmd/powershell cp1252)
if sys.stdout and hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
if sys.stderr and hasattr(sys.stderr, "reconfigure"):
    try:
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass

import typer

from support.__version__ import __version__
from support.core.config import AppConfig
from support.core.dialect import Dialect
from support.core.exceptions import (
    SupportError,
    InvalidPathError,
    UnreadableFileError,
    DatabaseAccessError,
    SQLParseError,
)
from support.core.logging import setup_logging
from support.project.scanner import ProjectScanner
from support.project.detector import ProjectDetector
from support.schema.sources.sql_file import SQLFileSchemaSource
from support.schema.sources.sqlite_db import SQLiteSchemaSource
from support.schema.merger import SchemaMerger
from support.schema.base import SchemaDiscoveryResult, DiscoveryWarning
from support.cli.display import (
    console,
    display_discovery_header,
    display_schema_summary,
    display_tables,
    display_diagnostics,
    display_error,
)
from support.cli.db_cmds import db_app

app = typer.Typer(
    name="support",
    help="Support: Multi-Database Schema Discovery and Inspection CLI.",
    add_completion=False,
)
app.add_typer(db_app, name="db")


def version_callback(value: bool) -> None:
    """Print version and exit."""
    if value:
        console.print(f"[bold cyan]Support[/bold cyan] version [bold white]{__version__}[/bold white]")
        raise typer.Exit()


@app.callback()
def main(
    version: Optional[bool] = typer.Option(
        None,
        "--version",
        "-V",
        help="Show application version and exit.",
        callback=version_callback,
        is_eager=True,
    ),
) -> None:
    """Support CLI root options."""
    pass


@app.command("version")
def version_cmd() -> None:
    """Display Support application version."""
    console.print(f"[bold cyan]Support[/bold cyan] version [bold white]{__version__}[/bold white]")


@app.command("inspect-sqlite")
def inspect_sqlite(
    database: Path = typer.Argument(
        ...,
        help="Path to an existing SQLite database file (.db, .sqlite).",
        exists=True,
        file_okay=True,
        dir_okay=False,
        readable=True,
        resolve_path=True,
    ),
    json_output: bool = typer.Option(
        False,
        "--json",
        help="Output normalized schema as JSON.",
    ),
) -> None:
    """Inspect an existing SQLite database file directly in read-only mode."""
    try:
        source = SQLiteSchemaSource(database)
        result = source.discover()

        if json_output:
            print(result.schema.model_dump_json(indent=2))
            return

        display_discovery_header(database, Dialect.SQLITE, result)
        display_schema_summary(result.schema)
        display_tables(result.schema)
        display_diagnostics(result)

    except SupportError as e:
        display_error("Inspection Error", e.message, e.details)
        sys.exit(1)
    except Exception as e:
        display_error("Unexpected Error", str(e))
        sys.exit(1)


@app.command("scan")
def scan(
    target: Path = typer.Argument(
        Path("."),
        help="Project directory or database file to scan.",
    ),
    dialect: Optional[str] = typer.Option(
        None,
        "--dialect",
        "-d",
        help="Target database dialect (postgres, mysql, sqlite, mssql).",
    ),
    sqlite_file: Optional[Path] = typer.Option(
        None,
        "--sqlite",
        help="Explicit SQLite database file to inspect alongside or instead of SQL files.",
    ),
    json_output: bool = typer.Option(
        False,
        "--json",
        help="Output discovered schema in JSON format.",
    ),
    verbose: bool = typer.Option(
        False,
        "--verbose",
        "-v",
        help="Enable detailed logging output.",
    ),
) -> None:
    """Scan a project directory or database for database schemas."""
    setup_logging(level="DEBUG" if verbose else "INFO")

    target_path = target.resolve()
    if not target_path.exists():
        display_error("Invalid Path", f"Target path '{target_path}' does not exist.")
        sys.exit(1)

    # Validate dialect if user passed one
    configured_dialect = Dialect.UNKNOWN
    if dialect:
        parsed_d = Dialect.from_string(dialect)
        if parsed_d == Dialect.UNKNOWN and dialect.lower() not in ("unknown", "generic"):
            display_error(
                "Invalid Dialect",
                f"Unknown dialect '{dialect}'.",
                "Supported dialects are: postgres, mysql, sqlite, mssql",
            )
            sys.exit(1)
        configured_dialect = parsed_d

    # Case 1: Target is directly a SQLite database file
    if target_path.is_file():
        try:
            source = SQLiteSchemaSource(target_path)
            result = source.discover()

            if json_output:
                print(result.schema.model_dump_json(indent=2))
                return

            display_discovery_header(target_path, Dialect.SQLITE, result)
            display_schema_summary(result.schema)
            display_tables(result.schema)
            display_diagnostics(result)
            return
        except SupportError as e:
            display_error("SQLite Inspection Error", e.message, e.details)
            sys.exit(1)

    # Case 2: Target is a directory
    try:
        scanner = ProjectScanner()
        discovered = scanner.scan(target_path)
    except SupportError as e:
        display_error("Scan Error", e.message, e.details)
        sys.exit(1)

    # Include explicit sqlite_file if provided
    sqlite_targets = list(discovered.sqlite_files)
    if sqlite_file:
        resolved_sqlite = sqlite_file.resolve()
        if not resolved_sqlite.exists() or not resolved_sqlite.is_file():
            display_error("Invalid SQLite Path", f"SQLite database '{resolved_sqlite}' does not exist.")
            sys.exit(1)
        if resolved_sqlite not in sqlite_targets:
            sqlite_targets.append(resolved_sqlite)

    # If no schema sources found at all
    if not discovered.sql_files and not sqlite_targets:
        display_error(
            "Empty Discovery Result",
            f"No SQL schema files (.sql, .ddl) or SQLite databases found in '{target_path}'.",
            "Ensure the directory contains database migration files or a valid SQLite database.",
        )
        sys.exit(1)

    # Detect dialect if not explicitly specified
    effective_dialect = ProjectDetector.detect_dialect(discovered, configured_dialect)

    # Run discovery on SQL files
    all_results: list[SchemaDiscoveryResult] = []
    if discovered.sql_files:
        sql_source = SQLFileSchemaSource(discovered.sql_files, dialect=effective_dialect)
        all_results.append(sql_source.discover())

    # Run discovery on SQLite files
    for sql_db in sqlite_targets:
        try:
            sqlite_source = SQLiteSchemaSource(sql_db)
            all_results.append(sqlite_source.discover())
        except SupportError as e:
            # Record failed file rather than crashing entire scan
            dummy_result = SchemaDiscoveryResult(schema=None)  # type: ignore
            dummy_result.discovered_files.append(sql_db)
            dummy_result.failed_files[str(sql_db)] = str(e)
            all_results.append(dummy_result)

    # Merge results
    valid_schemas = [r.schema for r in all_results if r.schema is not None]
    merged_schema, merge_warnings = SchemaMerger.merge(
        valid_schemas, forced_dialect=effective_dialect
    )

    combined_discovered = []
    combined_parsed = []
    combined_failed = {}
    combined_warnings = list(merge_warnings)

    for r in all_results:
        combined_discovered.extend(r.discovered_files)
        combined_parsed.extend(r.parsed_files)
        combined_failed.update(r.failed_files)
        combined_warnings.extend(r.warnings)

    final_result = SchemaDiscoveryResult(
        schema=merged_schema,
        discovered_files=combined_discovered,
        parsed_files=combined_parsed,
        failed_files=combined_failed,
        warnings=combined_warnings,
    )

    if json_output:
        print(final_result.schema.model_dump_json(indent=2))
        return

    display_discovery_header(target_path, final_result.schema.dialect, final_result)
    display_schema_summary(final_result.schema)
    display_tables(final_result.schema)
    display_diagnostics(final_result)


if __name__ == "__main__":
    app()
