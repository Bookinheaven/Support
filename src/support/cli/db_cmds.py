"""Database connection management and live inspection CLI commands."""

import os
from pathlib import Path
from typing import Optional
import typer

from support.core.dialect import Dialect
from support.core.exceptions import SupportError
from support.core.formatting import format_duration
from support.db.manager import ProfileManager, ProfileAlreadyExistsError, ProfileNotFoundError
from support.db.service import DatabaseService, ConnectionVerificationError
from support.schema.sources.live_db import LiveDatabaseSchemaSource
from support.cli.formatters import (
    console,
    print_step,
    print_success,
    print_warning,
    print_failure,
    render_profiles_table,
)
from support.cli.display import (
    display_discovery_header,
    display_schema_summary,
    display_tables,
    display_diagnostics,
)

db_app = typer.Typer(
    name="db",
    help="Manage database connection profiles and inspect live schemas.",
    no_args_is_help=True,
)


@db_app.command("add")
def db_add(
    url: Optional[str] = typer.Option(
        None,
        "--url",
        help="Database connection URL (e.g. postgresql://user:pass@localhost:5432/dbname).",
    ),
    name: Optional[str] = typer.Option(None, "--name", "-n", help="Unique profile name."),
    dialect: Optional[str] = typer.Option(
        None, "--dialect", "-d", help="Database dialect (postgres, mysql, sqlite, mssql)."
    ),
    host: Optional[str] = typer.Option(None, "--host", "-H", help="Database host address."),
    port: Optional[int] = typer.Option(None, "--port", "-p", help="Database port number."),
    database: Optional[str] = typer.Option(
        None, "--database", "-D", help="Database name or SQLite file path."
    ),
    username: Optional[str] = typer.Option(None, "--username", "-u", help="Database username."),
    password: Optional[str] = typer.Option(
        None, "--password", "-P", help="Database password (stored securely in OS credential store)."
    ),
    password_env: Optional[str] = typer.Option(
        None, "--password-env", help="Environment variable name holding the password."
    ),
    overwrite: bool = typer.Option(
        False, "--overwrite", help="Overwrite existing profile with the same name."
    ),
    test: bool = typer.Option(
        True, "--test/--no-test", help="Verify connection before saving the profile."
    ),
) -> None:
    """Register a new database connection profile (via URL, interactive prompts, or flags)."""
    service = DatabaseService()

    # Warn if credentials passed directly in CLI flags
    if (url and "@" in url and ":" in url.split("@")[0]) or password is not None:
        print_warning("Note: Passwords in command-line arguments may be recorded in shell history.")

    # Interactive prompt fallback when neither URL nor required parameters are provided
    if not url and not dialect and not database:
        if not name:
            name = typer.prompt("Profile name").strip()
            if not name:
                print_failure("Profile name cannot be empty.")
                raise typer.Exit(1)

        dialect_input = typer.prompt("Database dialect (postgres, mysql, sqlite, mssql)", default="postgres")
        parsed_d = Dialect.from_string(dialect_input)
        if parsed_d == Dialect.UNKNOWN:
            print_failure(f"Unsupported dialect '{dialect_input}'.", hint="Use postgres, mysql, sqlite, or mssql.")
            raise typer.Exit(1)
        dialect = parsed_d.value

        if parsed_d == Dialect.SQLITE:
            database = typer.prompt("SQLite database file path").strip()
        else:
            host = typer.prompt("Host", default="127.0.0.1").strip()
            default_port = {"postgres": 5432, "mysql": 3306, "mssql": 1433}.get(parsed_d.value, 5432)
            port = int(typer.prompt("Port", default=str(default_port)))
            database = typer.prompt("Database name").strip()
            username = typer.prompt("Username", default="support_dev").strip()
            if not password and not password_env:
                prompt_pw = typer.prompt("Password (hidden)", hide_input=True, default="", show_default=False)
                password = prompt_pw if prompt_pw else None

    # Step notice
    target_desc = url if url else f"{dialect}://{host or '127.0.0.1'}:{port or ''}/{database or ''}"
    if test:
        print_step(f"Connecting to database...")

    try:
        with console.status(f"[cyan]Registering profile '{name or 'database'}'...[/cyan]", spinner="dots"):
            profile, health_result = service.register_profile(
                url=url,
                name=name,
                dialect=dialect,
                host=host,
                port=port,
                database=database,
                username=username,
                password=password,
                password_env=password_env,
                overwrite=overwrite,
                test_connection=test,
            )

        if health_result and health_result.is_online:
            ver_part = f" · {health_result.server_version}" if health_result.server_version else ""
            lat_part = f" · {format_duration(duration_ms=health_result.latency_ms)}" if health_result.latency_ms is not None else ""
            print_success(f"Connection verified{ver_part}{lat_part}")
            print_success(f"Saved connection '{profile.name}'.")
        else:
            print_warning(f"Saved unverified connection '{profile.name}' (--no-test used; no connectivity check performed).")

    except ConnectionVerificationError as e:
        print_failure(f"Connection failed · {e.result.status}.", hint=e.result.hint)
        console.print("[dim]Connection not saved. Check the credentials and try again.[/dim]")
        raise typer.Exit(1)
    except ProfileAlreadyExistsError as e:
        print_failure(f"Profile '{e.profile_name}' already exists.", hint="Use --overwrite to update it or choose a different name.")
        raise typer.Exit(1)
    except SupportError as e:
        print_failure(e.message, details=e.details)
        raise typer.Exit(1)


@db_app.command("list")
def db_list(
    no_check: bool = typer.Option(
        False,
        "--no-check",
        help="Display saved profiles immediately without probing live connectivity.",
    ),
    timeout: float = typer.Option(
        3.0,
        "--timeout",
        help="Timeout in seconds per database connection check.",
    ),
) -> None:
    """List all saved database profiles with live connectivity status."""
    service = DatabaseService()
    if not no_check:
        with console.status("[cyan]Checking database connections...[/cyan]", spinner="dots"):
            profiles, health_results = service.list_profiles_with_status(
                check_health=True, timeout=timeout
            )
    else:
        profiles, health_results = service.list_profiles_with_status(
            check_health=False, timeout=timeout
        )
    render_profiles_table(
        profiles,
        health_results=health_results,
        no_check=no_check,
        default_profile=service.get_default_profile(),
    )


@db_app.command("use")
def db_use(
    profile_name: str = typer.Argument(..., help="Name of the saved connection profile to set as active default."),
) -> None:
    """Set the active default database profile for sessions and workspace."""
    service = DatabaseService()
    try:
        service.set_default_profile(profile_name)
        print_success(f"Default database profile set to '{profile_name}'.")
    except ProfileNotFoundError:
        print_failure(f"Profile '{profile_name}' not found.", hint="Use 'support db list' to view available profiles.")
        raise typer.Exit(1)
    except SupportError as e:
        print_failure(e.message, details=e.details)
        raise typer.Exit(1)


@db_app.command("test")
def db_test(
    profile_name: str = typer.Argument(..., help="Name of the saved connection profile to test."),
    timeout: float = typer.Option(5.0, "--timeout", help="Connection timeout in seconds."),
) -> None:
    """Test connectivity to a saved database profile."""
    service = DatabaseService()
    try:
        profile = service.manager.get_profile(profile_name)
        if not profile:
            raise ProfileNotFoundError(profile_name)

        with console.status(f"[cyan]Connecting to '{profile.name}' ({profile.get_redacted_url()})...[/cyan]", spinner="dots"):
            _, result = service.test_profile(profile_name, timeout=timeout)

        if result.is_online:
            ver_part = f" · {result.server_version}" if result.server_version else ""
            lat_part = f" · {format_duration(duration_ms=result.latency_ms)}" if result.latency_ms is not None else ""
            print_success(f"Connected successfully{ver_part}{lat_part}")
        else:
            print_failure(f"Connection failed · {result.status}.", details=result.error_message, hint=result.hint)
            raise typer.Exit(1)

    except ProfileNotFoundError:
        print_failure(f"Profile '{profile_name}' not found.", hint="Use 'support db list' to view available profiles.")
        raise typer.Exit(1)
    except SupportError as e:
        print_failure(e.message, details=e.details)
        raise typer.Exit(1)


@db_app.command("inspect")
def db_inspect(
    profile_name: str = typer.Argument(..., help="Name of the saved connection profile to inspect."),
    json_output: bool = typer.Option(False, "--json", help="Output schema in JSON format."),
) -> None:
    """Inspect the live schema of a saved database connection."""
    service = DatabaseService()
    try:
        profile, password = service.manager.get_profile_with_password(profile_name)
    except ProfileNotFoundError:
        print_failure(f"Profile '{profile_name}' not found.", hint="Use 'support db list' to view available profiles.")
        raise typer.Exit(1)

    try:
        if profile.dialect == Dialect.MONGODB:
            from support.schema.sources.mongo_db import MongoDatabaseSchemaSource
            from support.cli.display import display_mongo_schema

            with console.status(f"[cyan]Inspecting MongoDB collections in '{profile.name}'...[/cyan]", spinner="dots"):
                mongo_source = MongoDatabaseSchemaSource(profile, password=password)
                mongo_schema = mongo_source.discover()

            if json_output:
                print(mongo_schema.model_dump_json(indent=2))
                return

            display_mongo_schema(mongo_schema)
            return

        with console.status(f"[cyan]Inspecting database schema for '{profile.name}'...[/cyan]", spinner="dots"):
            source = LiveDatabaseSchemaSource(profile, password=password)
            result = source.discover()

        if json_output:
            print(result.schema.model_dump_json(indent=2))
            return

        display_discovery_header(Path(profile.display_target), profile.dialect, result)
        display_schema_summary(result.schema)
        display_tables(result.schema)
        display_diagnostics(result)

    except SupportError as e:
        print_failure(f"Schema inspection failed: {e.message}", details=e.details)
        raise typer.Exit(1)
    except Exception as e:
        print_failure(f"Unexpected inspection error: {e}")
        raise typer.Exit(1)


@db_app.command("remove")
def db_remove(
    profile_name: str = typer.Argument(..., help="Name of the saved connection profile to remove."),
    yes: bool = typer.Option(False, "--yes", "-y", help="Confirm removal without prompting."),
) -> None:
    """Remove a saved connection profile and its keyring credentials."""
    service = DatabaseService()
    try:
        profile = service.manager.get_profile(profile_name)
        if not profile:
            raise ProfileNotFoundError(profile_name)

        if not yes:
            confirm = typer.confirm(f"Remove connection profile '{profile_name}'? (The database will not be affected)")
            if not confirm:
                console.print("[dim]Aborted.[/dim]")
                return

        service.remove_profile(profile_name)
        print_success(f"Removed connection profile '{profile_name}'.")

    except ProfileNotFoundError:
        print_failure(f"Profile '{profile_name}' not found.", hint="Use 'support db list' to view available profiles.")
        raise typer.Exit(1)
    except SupportError as e:
        print_failure(e.message, details=e.details)
        raise typer.Exit(1)


@db_app.command("diagram")
def db_diagram(
    profile_name: str = typer.Argument(..., help="Name of the saved connection profile to diagram."),
    output: Optional[Path] = typer.Option(
        None,
        "--output",
        "-o",
        help="Custom output file path (.mmd). Defaults to ~/.support/diagrams/<profile>.mmd",
    ),
    tables: Optional[str] = typer.Option(
        None,
        "--tables",
        "-t",
        help="Optional comma-separated list of tables to include (e.g. 'users,orders').",
    ),
    overwrite: bool = typer.Option(
        False,
        "--overwrite",
        "-y",
        help="Overwrite existing output file without prompting.",
    ),
    html: bool = typer.Option(
        False,
        "--html",
        help="Also generate an accompanying standalone HTML interactive diagram viewer.",
    ),
) -> None:
    """Generate a Mermaid ER diagram (.mmd) for a database connection profile."""
    service = DatabaseService()
    try:
        profile, password = service.manager.get_profile_with_password(profile_name)
    except ProfileNotFoundError:
        print_failure(f"Profile '{profile_name}' not found.", hint="Use 'support db list' to view available profiles.")
        raise typer.Exit(1)

    if profile.dialect == Dialect.MONGODB:
        print_warning(
            f"Profile '{profile.name}' is MongoDB. Relational ER diagrams are designed for relational databases (PostgreSQL, MySQL, SQLite, SQL Server)."
        )
        console.print(f"  [dim]To inspect document collections and schema summaries, run: support db inspect {profile.name}[/dim]")
        raise typer.Exit(0)

    try:
        from support.schema.diagram import generate_mermaid_er, save_diagram_file

        with console.status(f"[cyan]Discovering schema for '{profile.name}'...[/cyan]", spinner="dots"):
            source = LiveDatabaseSchemaSource(profile, password=password)
            result = source.discover()

        table_filter = [t.strip() for t in tables.split(",")] if tables else None

        mermaid_code = generate_mermaid_er(
            result.schema,
            table_filter=table_filter,
            title=f"ER Diagram · {profile.name} ({profile.dialect.value})",
        )

        try:
            mmd_path, html_path = save_diagram_file(
                mermaid_code,
                output_path=output,
                profile_name=profile.name,
                overwrite=overwrite,
                generate_html=html,
            )
        except FileExistsError as e:
            print_failure(str(e), hint="Pass --overwrite (-y) to replace the existing file.")
            raise typer.Exit(1)

        print_success(f"Generated Mermaid ER diagram for '{profile.name}'")
        console.print(f"  [bold white]Diagram path:[/bold white] {mmd_path}")
        if html_path:
            console.print(f"  [bold white]HTML preview:[/bold white] {html_path}")
        console.print("  [dim]Open with any Mermaid previewer, VS Code extension, or paste into https://mermaid.live[/dim]")

    except SupportError as e:
        print_failure(f"Diagram generation failed: {e.message}", details=e.details)
        raise typer.Exit(1)
    except typer.Exit:
        raise
    except Exception as e:
        print_failure(f"Unexpected diagram generation error: {e}")
        raise typer.Exit(1)

