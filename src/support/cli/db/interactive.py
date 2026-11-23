"""Interactive database connection management flows using Questionary."""

from __future__ import annotations

import re
import sys
import time
from typing import TYPE_CHECKING

import questionary
import typer

from support.application.errors import (
    AuthenticationFailedError,
    CredentialError,
    DatabaseConnectionError,
    ProfileError,
    ProfileNotFoundError,
)
from support.cli.composition import (
    get_connection_workflow,
    get_database_connector,
    get_profile_service,
)
from support.cli.presentation import format_duration
from support.domain.profiles import (
    ConnectionConfig,
    DatabaseProfile,
    DatabaseType,
    MariaDBConnection,
    MongoDBConnection,
    MySQLConnection,
    PostgresConnection,
    SQLiteConnection,
    SQLServerConnection,
    requires_credentials,
)


def _format_connection_summary(profile: DatabaseProfile) -> str:
    """Format one-line summary for profile listing."""
    conn = profile.connection
    db_type = conn.database_type.value.capitalize()
    if isinstance(conn, SQLiteConnection):
        return f"{profile.name} ({db_type} · {conn.database_path})"
    elif isinstance(conn, MongoDBConnection):
        host = f"{conn.host}:{conn.port}" if conn.port else conn.host
        return f"{profile.name} ({db_type} · {host} · db: {conn.database})"
    else:
        return f"{profile.name} ({db_type} · {conn.host}:{conn.port} · db: {conn.database})"


def _parse_port(port_str: str | None, default: int) -> int:
    """Parse port string with fallback to default."""
    if not port_str:
        return default
    try:
        p = int(port_str.strip())
        return p if 1 <= p <= 65535 else default
    except ValueError:
        return default


def compact_add_connection() -> str | None:
    """Compact interactive wizard to add a new database connection.

    Tests connectivity before saving to prevent orphaned records.
    Returns the created profile name, or None if cancelled.
    """
    service = get_profile_service()

    def validate_name(val: str) -> bool | str:
        s = val.strip().lower()
        if not s:
            return "Profile name cannot be empty."
        if not re.match(r"^[a-z0-9_-]{1,64}$", s):
            return "Name must be 1-64 characters (lowercase alphanumeric, hyphens, underscores)."
        try:
            service.get_profile(s)
            return f"Profile '{s}' already exists."
        except ProfileNotFoundError:
            pass
        return True

    try:
        db_choice = questionary.select(
            "Database type:",
            choices=[
                "PostgreSQL",
                "MySQL",
                "MariaDB",
                "SQLite",
                "Microsoft SQL Server",
                "MongoDB",
            ],
        ).ask()
    except (KeyboardInterrupt, EOFError):
        db_choice = None

    if not db_choice:
        typer.echo("\nSetup cancelled.\n")
        return None

    try:
        name_input = questionary.text(
            "Profile name:",
            validate=validate_name,
        ).ask()
    except (KeyboardInterrupt, EOFError):
        name_input = None

    if not name_input:
        typer.echo("\nSetup cancelled.\n")
        return None

    name = name_input.strip().lower()

    conn_config: ConnectionConfig
    password: str | None = None

    try:
        if db_choice == "PostgreSQL":
            host = questionary.text("Host:", default="localhost").ask()
            if host is None:
                return None
            port_str = questionary.text("Port:", default="5432").ask()
            if port_str is None:
                return None
            database = questionary.text("Database:", default="postgres").ask()
            if database is None:
                return None
            username = questionary.text("Username:", default="postgres").ask()
            if username is None:
                return None
            password = questionary.password("Password:").ask()
            if password is None:
                return None
            ssl_mode = questionary.select(
                "SSL Mode:",
                choices=["prefer", "require", "disable", "verify-ca", "verify-full"],
                default="prefer",
            ).ask()
            if ssl_mode is None:
                return None
            conn_config = PostgresConnection(
                host=host.strip(),
                port=_parse_port(port_str, 5432),
                database=database.strip(),
                username=username.strip(),
                ssl_mode=ssl_mode,
            )

        elif db_choice in ("MySQL", "MariaDB"):
            host = questionary.text("Host:", default="localhost").ask()
            if host is None:
                return None
            port_str = questionary.text("Port:", default="3306").ask()
            if port_str is None:
                return None
            database = questionary.text("Database:", default="app").ask()
            if database is None:
                return None
            username = questionary.text("Username:", default="root").ask()
            if username is None:
                return None
            password = questionary.password("Password:").ask()
            if password is None:
                return None
            if db_choice == "MySQL":
                ssl_choices = ["preferred", "required", "disabled", "verify_ca", "verify_identity"]
                ssl_default = "preferred"
            else:
                ssl_choices = ["prefer", "require", "disable", "verify-ca", "verify-full"]
                ssl_default = "prefer"

            ssl_mode = questionary.select(
                "SSL Mode:",
                choices=ssl_choices,
                default=ssl_default,
            ).ask()
            if ssl_mode is None:
                return None

            cls = MySQLConnection if db_choice == "MySQL" else MariaDBConnection
            conn_config = cls(
                host=host.strip(),
                port=_parse_port(port_str, 3306),
                database=database.strip(),
                username=username.strip(),
                ssl_mode=ssl_mode,
            )

        elif db_choice == "SQLite":
            db_path = questionary.text("Database path:", default="./data.db").ask()
            if db_path is None:
                return None
            conn_config = SQLiteConnection(database_path=db_path.strip())
            password = None

        elif db_choice == "Microsoft SQL Server":
            host = questionary.text("Host:", default="localhost").ask()
            if host is None:
                return None
            port_str = questionary.text("Port:", default="1433").ask()
            if port_str is None:
                return None
            database = questionary.text("Database:", default="master").ask()
            if database is None:
                return None
            username = questionary.text("Username:", default="sa").ask()
            if username is None:
                return None
            password = questionary.password("Password:").ask()
            if password is None:
                return None
            encrypt = questionary.confirm("Enable encryption?", default=True).ask()
            if encrypt is None:
                return None
            trust_cert = questionary.confirm("Trust server certificate?", default=False).ask()
            if trust_cert is None:
                return None
            conn_config = SQLServerConnection(
                host=host.strip(),
                port=_parse_port(port_str, 1433),
                database=database.strip(),
                username=username.strip(),
                encrypt=encrypt,
                trust_server_certificate=trust_cert,
            )

        elif db_choice == "MongoDB":
            host = questionary.text("Host:", default="localhost").ask()
            if host is None:
                return None
            port_str = questionary.text("Port:", default="27017").ask()
            if port_str is None:
                return None
            database = questionary.text("Database:", default="test").ask()
            if database is None:
                return None
            username_in = questionary.text("Username (leave blank for unauthenticated):", default="").ask()
            if username_in is None:
                return None
            user_val = username_in.strip() or None
            if user_val:
                password = questionary.password("Password:").ask()
                if password is None:
                    return None
            else:
                password = None
            direct_conn = questionary.confirm("Direct connection?", default=False).ask()
            if direct_conn is None:
                return None
            h = host.strip()
            p = _parse_port(port_str, 27017)
            uri = f"mongodb://{h}:{p}"
            conn_config = MongoDBConnection(
                uri=uri,
                database=database.strip(),
                username=user_val,
                direct_connection=direct_conn,
            )
        else:
            return None

    except (KeyboardInterrupt, EOFError):
        typer.echo("\nSetup cancelled.\n")
        return None

    # Test connection BEFORE permanently saving
    dispatcher = get_database_connector()
    while True:
        typer.echo(f"\nTesting connection for '{name}'...")
        t0 = time.perf_counter()
        try:
            result = dispatcher.test_connection(conn_config, password=password)
            duration = time.perf_counter() - t0
            typer.echo("✓ Connection successful.")
            if result.database_type:
                typer.echo(f"  Type:        {result.database_type}")
            if result.database:
                typer.echo(f"  Database:    {result.database}")
            if result.server_version:
                typer.echo(f"  Server:      {result.database_type.capitalize()} {result.server_version}")
            typer.echo(f"  Connect time: {format_duration(duration)}")

            save_pw = False
            if password is not None:
                try:
                    save_pw = questionary.confirm("Save password securely?", default=True).ask()
                except (KeyboardInterrupt, EOFError):
                    save_pw = False

            created_profile = service.add_profile(name=name, connection=conn_config)
            if save_pw and password is not None:
                try:
                    service.set_profile_password(name, password)
                except CredentialError as exc:
                    typer.echo(f"Warning: Could not save password to keyring: {exc}", err=True)

            typer.echo(f"\n✓ Connection '{name}' saved.")
            typer.echo(f"Tip: You can test this connection anytime with 'support db test {name}'.\n")
            return name

        except (DatabaseConnectionError, AuthenticationFailedError, Exception) as exc:
            typer.echo(f"\n✗ Connection failed: {exc}", err=True)
            try:
                action = questionary.select(
                    "What would you like to do?",
                    choices=["Retry credentials", "Save connection anyway", "Cancel"],
                ).ask()
            except (KeyboardInterrupt, EOFError):
                action = "Cancel"

            if action == "Retry credentials":
                if isinstance(
                    conn_config,
                    (
                        PostgresConnection,
                        MySQLConnection,
                        MariaDBConnection,
                        SQLServerConnection,
                        MongoDBConnection,
                    ),
                ):
                    try:
                        new_user = questionary.text(
                            "Username:",
                            default=getattr(conn_config, "username", "") or "",
                        ).ask()
                        new_pw = questionary.password("Password:").ask()
                    except (KeyboardInterrupt, EOFError):
                        typer.echo("\nConnection discarded.\n")
                        return None
                    if new_user:
                        conn_config = conn_config.model_copy(update={"username": new_user.strip()})
                    password = new_pw if new_pw else None
                    continue
                else:
                    continue
            elif action == "Save connection anyway":
                save_pw = False
                if password is not None:
                    try:
                        save_pw = questionary.confirm("Save password securely?", default=True).ask()
                    except (KeyboardInterrupt, EOFError):
                        save_pw = False

                created_profile = service.add_profile(name=name, connection=conn_config)
                if save_pw and password is not None:
                    try:
                        service.set_profile_password(name, password)
                    except CredentialError as exc:
                        typer.echo(f"Warning: Could not save password: {exc}", err=True)
                typer.echo(f"\n✓ Connection '{name}' saved.")
                typer.echo(f"Tip: You can test this connection anytime with 'support db test {name}'.\n")
                return name
            else:
                typer.echo("Connection discarded.\n")
                return None


def interactive_list_connections() -> None:
    """List all registered connections in compact format."""
    service = get_profile_service()
    profiles = service.list_profiles()
    if not profiles:
        typer.echo("\nNo database connections configured.\n")
        return

    typer.echo("\nRegistered connections:")
    for p in profiles:
        typer.echo(f"  • {_format_connection_summary(p)}")
    typer.echo("\nTip: View connection details with 'support db show <name>'.\n")


def interactive_test_connection() -> None:
    """Interactive selector to test a connection."""
    service = get_profile_service()
    profiles = service.list_profiles()
    if not profiles:
        typer.echo("\nNo database connections configured.\n")
        return

    choices = [p.name for p in profiles]
    try:
        selected = questionary.select("Select connection to test:", choices=choices).ask()
    except (KeyboardInterrupt, EOFError):
        selected = None

    if not selected:
        return

    workflow = get_connection_workflow()
    profile = workflow.get_profile(selected)
    conn = profile.connection
    password = None
    used_saved = False

    if _requires_credentials(conn):
        try:
            password = workflow.resolve_saved_password(profile.id)
            used_saved = password is not None
        except Exception:
            used_saved = False

        if not used_saved:
            try:
                pw_input = questionary.password(
                    f"Enter password for '{selected}' (leave blank for none):"
                ).ask()
                password = pw_input if pw_input else None
            except (KeyboardInterrupt, EOFError):
                return

    typer.echo(f"\nTesting connection for '{selected}'...")
    t0 = time.perf_counter()
    try:
        res = workflow.test_profile_connection(
            selected,
            password=password,
            used_saved_password=used_saved,
        )
        duration = time.perf_counter() - t0
        typer.echo("✓ Connection successful.")
        if res.database_type:
            typer.echo(f"  Type:     {res.database_type}")
        if res.database:
            typer.echo(f"  Database: {res.database}")
        if res.server_version:
            typer.echo(f"  Server:   {res.database_type.capitalize()} {res.server_version}")
        typer.echo(f"  Connect time: {format_duration(duration)}")
        typer.echo(f"Tip: Test directly via CLI with 'support db test {selected}'.\n")
    except Exception as exc:
        typer.echo(f"✗ Connection failed: {exc}\n", err=True)


def interactive_remove_connection() -> None:
    """Interactive selector to remove a connection."""
    service = get_profile_service()
    profiles = service.list_profiles()
    if not profiles:
        typer.echo("\nNo database connections configured.\n")
        return

    choices = [p.name for p in profiles]
    try:
        selected = questionary.select("Select connection to remove:", choices=choices).ask()
    except (KeyboardInterrupt, EOFError):
        selected = None

    if not selected:
        return

    try:
        confirm = questionary.confirm(
            f"Remove connection '{selected}' and any stored credentials?",
            default=False,
        ).ask()
    except (KeyboardInterrupt, EOFError):
        confirm = False

    if confirm:
        service.remove_profile(selected)
        typer.echo(f"✓ Connection '{selected}' removed.")
        typer.echo(f"Tip: Remove connections via CLI with 'support db remove {selected} --yes'.\n")
    else:
        typer.echo("Removal cancelled.\n")


def run_db_management() -> None:
    """Launch the compact interactive database management menu."""
    while True:
        try:
            action = questionary.select(
                "Database Management:",
                choices=[
                    "Add connection",
                    "List connections",
                    "Test connection",
                    "Remove connection",
                    "Exit",
                ],
            ).ask()
        except (KeyboardInterrupt, EOFError):
            action = "Exit"

        if not action or action == "Exit":
            break
        elif action == "Add connection":
            compact_add_connection()
        elif action == "List connections":
            interactive_list_connections()
        elif action == "Test connection":
            interactive_test_connection()
        elif action == "Remove connection":
            interactive_remove_connection()
