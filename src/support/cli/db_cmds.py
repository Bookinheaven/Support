"""Database connection management and live inspection CLI commands."""

import os
from pathlib import Path
from typing import Optional
import typer

from support.core.dialect import Dialect
from support.core.exceptions import SupportError
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
            lat_part = f" · {health_result.latency_ms:.0f} ms" if health_result.latency_ms is not None else ""
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
    profiles, health_results = service.list_profiles_with_status(
        check_health=not no_check, timeout=timeout
    )
    render_profiles_table(profiles, health_results=health_results, no_check=no_check)


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

        print_step(f"Connecting to '{profile.name}' ({profile.get_redacted_url()})...")
        _, result = service.test_profile(profile_name, timeout=timeout)

        if result.is_online:
            ver_part = f" · {result.server_version}" if result.server_version else ""
            lat_part = f" · {result.latency_ms:.0f} ms" if result.latency_ms is not None else ""
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
