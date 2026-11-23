"""Database connection testing CLI commands."""

import sys
import time
import typer

from support.application.errors import (
    AuthenticationFailedError,
    CredentialError,
    DatabaseConnectionError,
    ProfileError,
)
from support.cli.composition import get_connection_workflow
from support.cli.presentation import format_duration, get_console
from support.domain.profiles import ConnectionConfig, DatabaseType, requires_credentials


def _is_interactive() -> bool:
    """Check whether stdin is attached to an interactive terminal."""
    return sys.stdin.isatty()


def test_profile(
    name: str = typer.Argument(..., help="Name of the profile to test."),
) -> None:
    """Establish connection, run test query, and report result without exposing credentials."""
    workflow = get_connection_workflow()
    try:
        profile = workflow.get_profile(name)
        conn = profile.connection

        password = None
        used_saved = False

        if requires_credentials(conn):
            # 1. Check for saved credential
            try:
                password = workflow.resolve_saved_password(profile.id)
                used_saved = password is not None
            except CredentialError as exc:
                if not _is_interactive():
                    typer.echo(
                        f"Error: Failed to retrieve saved password for profile '{name}': {exc}",
                        err=True,
                    )
                    raise typer.Exit(code=1)
                typer.echo(
                    f"Warning: Could not retrieve saved password from secure store: {exc}",
                    err=True,
                )
                typer.echo("Falling back to one-time password entry for this connection test.")
                used_saved = False

            # 2. If no saved password, prompt for one-time password
            if not used_saved and password is None:
                if not _is_interactive():
                    typer.echo(
                        f"Error: No saved password for profile '{name}' and non-interactive input is not supported.\n"
                        f"Next step: Store a password using: support profile password set {name}",
                        err=True,
                    )
                    raise typer.Exit(code=1)

                prompt_pw = typer.prompt(
                    f"Enter password for profile '{name}' (press Enter for none)",
                    default="",
                    hide_input=True,
                    show_default=False,
                )
                password = prompt_pw if prompt_pw else None

        # 3. Test connection with spinner and timing
        console = get_console()
        t0 = time.perf_counter()
        if console.is_terminal and sys.stdout.isatty():
            with console.status(f"Testing connection to '{name}'...", spinner="dots"):
                result = workflow.test_profile_connection(
                    name,
                    password=password,
                    used_saved_password=used_saved,
                )
        else:
            result = workflow.test_profile_connection(
                name,
                password=password,
                used_saved_password=used_saved,
            )
        duration = time.perf_counter() - t0

        typer.echo("Connection succeeded.")
        typer.echo(f"  Profile:     {name}")
        typer.echo(f"  Type:        {result.database_type}")
        typer.echo(f"  Database:    {result.database}")
        if result.username:
            typer.echo(f"  User:        {result.username}")
        if result.host and result.port:
            typer.echo(f"  Host:        {result.host}:{result.port}")
        elif result.host:
            typer.echo(f"  Host:        {result.host}")
        if result.ssl_mode:
            typer.echo(f"  SSL Mode:    {result.ssl_mode}")
        if result.server_version:
            if result.database_type == "postgresql":
                typer.echo(f"  Server:      PostgreSQL {result.server_version}")
            elif result.database_type == "sqlite":
                typer.echo(f"  Server:      SQLite {result.server_version}")
            elif result.database_type == "mysql":
                typer.echo(f"  Server:      MySQL {result.server_version}")
            elif result.database_type == "mariadb":
                typer.echo(f"  Server:      MariaDB {result.server_version}")
            else:
                typer.echo(f"  Server:      {result.database_type.capitalize()} {result.server_version}")
        typer.echo(f"  Connect time: {format_duration(duration)}")

    except AuthenticationFailedError as exc:
        typer.echo(f"Error: {exc}", err=True)
        if exc.used_saved_password:
            typer.echo(
                f"Next step: Update the saved password with: support profile password set {name}",
                err=True,
            )
        else:
            typer.echo(
                f"Next step: Verify your credentials or save a password with: support profile password set {name}",
                err=True,
            )
        typer.echo(f"Tip: Run 'support db test {name}' again after fixing credentials.", err=True)
        raise typer.Exit(code=1)
    except (DatabaseConnectionError, ProfileError, CredentialError) as exc:
        typer.echo(f"Error: {exc}", err=True)
        typer.echo(f"Tip: Run 'support db test {name}' again after fixing the issue.", err=True)
        raise typer.Exit(code=1)
