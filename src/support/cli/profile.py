"""Profile management CLI commands for Support."""

import sys
from pathlib import Path
import typer

from support.application.connection import ConnectionTestWorkflow
from support.application.errors import (
    AuthenticationFailedError,
    ConnectionError,
    CredentialError,
    CredentialStoreUnavailableError,
    ProfileAlreadyExistsError,
    ProfileError,
    ProfileNotFoundError,
    ProfileStorageError,
)
from support.application.ports import CredentialStore, DatabaseConnector
from support.application.service import ProfileService
from support.domain.profiles import (
    DatabaseType,
    MariaDBConnection,
    MongoDBConnection,
    MySQLConnection,
    PostgresConnection,
    ProfileValidationError,
    SQLiteConnection,
    SQLServerConnection,
)
from support.infrastructure.dispatcher import ConnectorDispatcher
from support.infrastructure.keyring_credentials import KeyringCredentialStore
from support.infrastructure.sqlite_profiles import SQLiteProfileRepository

profile_app = typer.Typer(
    name="profile",
    help="Manage database connection profiles.",
    no_args_is_help=True,
)
password_app = typer.Typer(
    name="password",
    help="Manage stored database profile passwords.",
    no_args_is_help=True,
)
profile_app.add_typer(password_app, name="password")


def _get_service(credential_store: CredentialStore | None = None) -> ProfileService:
    repository = SQLiteProfileRepository()
    if credential_store is None:
        credential_store = KeyringCredentialStore()
    return ProfileService(repository=repository, credential_store=credential_store)


def _get_connection_workflow(
    connector: DatabaseConnector | None = None,
    credential_store: CredentialStore | None = None,
) -> ConnectionTestWorkflow:
    repository = SQLiteProfileRepository()
    if connector is None:
        connector = ConnectorDispatcher()
    if credential_store is None:
        credential_store = KeyringCredentialStore()
    return ConnectionTestWorkflow(
        repository=repository,
        connector=connector,
        credential_store=credential_store,
    )


@profile_app.command(name="add", help="Add a new database profile.")
def add_profile(
    name: str = typer.Option(
        ...,
        "--name",
        help="Unique profile name (1-64 chars, lowercase alphanumeric, hyphens, underscores).",
    ),
    type: str = typer.Option(
        "postgresql",
        "--type",
        "-t",
        help="Database type (postgresql, mysql, mariadb, sqlite, sqlserver, mongodb).",
    ),
    database: str | None = typer.Option(None, "--database", help="Target database name."),
    username: str | None = typer.Option(None, "--username", help="Database login username."),
    host: str = typer.Option("localhost", "--host", help="Database host or IP address."),
    port: int | None = typer.Option(None, "--port", help="Database port (1-65535)."),
    ssl_mode: str | None = typer.Option(
        None,
        "--ssl-mode",
        help="SSL mode (for postgresql, mysql, mariadb).",
    ),
    database_path: str | None = typer.Option(
        None,
        "--database-path",
        help="File system path to SQLite database (for sqlite).",
    ),
    auth_source: str = typer.Option("admin", "--auth-source", help="MongoDB auth source (default: admin)."),
    tls: bool = typer.Option(False, "--tls", help="Enable TLS (for mongodb)."),
    encrypt: bool = typer.Option(True, "--encrypt", help="Enable encryption (for sqlserver)."),
    trust_server_certificate: bool = typer.Option(
        False, "--trust-server-certificate", help="Trust server certificate (for sqlserver)."
    ),
) -> None:
    """Add a new database profile."""
    service = _get_service()
    try:
        db_type_str = type.strip().lower()
        try:
            db_type = DatabaseType(db_type_str)
        except ValueError:
            supported = ", ".join(t.value for t in DatabaseType)
            raise ProfileValidationError(
                f"Unsupported database type '{type}'. Supported types are: {supported}."
            )

        if db_type == DatabaseType.POSTGRESQL:
            if database is None:
                raise ProfileValidationError("--database is required for postgresql profiles.")
            if username is None:
                raise ProfileValidationError("--username is required for postgresql profiles.")
            if ssl_mode is None:
                raise ProfileValidationError("--ssl-mode is required for postgresql profiles.")
            connection = PostgresConnection(
                database=database,
                username=username,
                ssl_mode=ssl_mode,
                host=host,
                port=port if port is not None else 5432,
            )
        elif db_type == DatabaseType.MYSQL:
            if database is None:
                raise ProfileValidationError("--database is required for mysql profiles.")
            if username is None:
                raise ProfileValidationError("--username is required for mysql profiles.")
            connection = MySQLConnection(
                database=database,
                username=username,
                host=host,
                port=port if port is not None else 3306,
                ssl_mode=ssl_mode or "preferred",
            )
        elif db_type == DatabaseType.MARIADB:
            if database is None:
                raise ProfileValidationError("--database is required for mariadb profiles.")
            if username is None:
                raise ProfileValidationError("--username is required for mariadb profiles.")
            connection = MariaDBConnection(
                database=database,
                username=username,
                host=host,
                port=port if port is not None else 3306,
                ssl_mode=ssl_mode or "prefer",
            )
        elif db_type == DatabaseType.SQLITE:
            resolved_path = database_path or database
            if not resolved_path:
                raise ProfileValidationError(
                    "--database-path (or --database) is required for sqlite profiles."
                )
            connection = SQLiteConnection(database_path=resolved_path)
        elif db_type == DatabaseType.SQLSERVER:
            if database is None:
                raise ProfileValidationError("--database is required for sqlserver profiles.")
            if username is None:
                raise ProfileValidationError("--username is required for sqlserver profiles.")
            connection = SQLServerConnection(
                database=database,
                username=username,
                host=host,
                port=port if port is not None else 1433,
                encrypt=encrypt,
                trust_server_certificate=trust_server_certificate,
            )
        elif db_type == DatabaseType.MONGODB:
            if database is None:
                raise ProfileValidationError("--database is required for mongodb profiles.")
            connection = MongoDBConnection(
                database=database,
                username=username,
                host=host,
                port=port if port is not None else 27017,
                auth_source=auth_source,
                tls=tls,
            )

        profile = service.add_profile(name=name, connection=connection)
        typer.echo(f"Profile '{profile.name}' added successfully.")
    except (ProfileValidationError, ProfileError) as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(code=1)


@profile_app.command(name="list", help="List all saved database profiles.")
def list_profiles() -> None:
    """List all saved database profiles."""
    service = _get_service()
    try:
        profiles = service.list_profiles()
    except ProfileError as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(code=1)

    if not profiles:
        typer.echo("No profiles found. Use 'support profile add' to create one.")
        return

    headers = ("NAME", "TYPE", "HOST", "DATABASE", "USERNAME")
    rows = []
    for p in profiles:
        conn = p.connection
        db_type = conn.database_type.value
        if isinstance(conn, SQLiteConnection):
            host_str = "-"
            db_str = conn.database_path
            user_str = "-"
        elif isinstance(conn, MongoDBConnection):
            host_str = f"{conn.host}:{conn.port}"
            db_str = conn.database
            user_str = conn.username or "-"
        else:
            host_str = f"{conn.host}:{conn.port}"
            db_str = conn.database
            user_str = getattr(conn, "username", "-")
        rows.append((p.name, db_type, host_str, db_str, user_str))

    col_widths = [len(h) for h in headers]
    for row in rows:
        for i, val in enumerate(row):
            col_widths[i] = max(col_widths[i], len(str(val)))

    header_line = "  ".join(h.ljust(col_widths[i]) for i, h in enumerate(headers))
    typer.echo(header_line)
    for row in rows:
        row_line = "  ".join(str(val).ljust(col_widths[i]) for i, val in enumerate(row))
        typer.echo(row_line)


@profile_app.command(name="show", help="Display details of a saved database profile.")
def show_profile(
    name: str = typer.Argument(..., help="Name of the profile to display."),
) -> None:
    """Display details of a saved database profile."""
    service = _get_service()
    try:
        profile = service.get_profile(name)
    except ProfileError as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(code=1)

    conn = profile.connection
    typer.echo(f"Profile: {profile.name}")
    typer.echo(f"  ID:          {profile.id}")
    typer.echo(f"  Type:        {conn.database_type.value}")
    if isinstance(conn, SQLiteConnection):
        typer.echo(f"  Path:        {conn.database_path}")
    elif isinstance(conn, PostgresConnection):
        typer.echo(f"  Host:        {conn.host}")
        typer.echo(f"  Port:        {conn.port}")
        typer.echo(f"  Database:    {conn.database}")
        typer.echo(f"  Username:    {conn.username}")
        typer.echo(f"  SSL Mode:    {conn.ssl_mode}")
    elif isinstance(conn, (MySQLConnection, MariaDBConnection)):
        typer.echo(f"  Host:        {conn.host}")
        typer.echo(f"  Port:        {conn.port}")
        typer.echo(f"  Database:    {conn.database}")
        typer.echo(f"  Username:    {conn.username}")
        typer.echo(f"  SSL Mode:    {conn.ssl_mode}")
    elif isinstance(conn, SQLServerConnection):
        typer.echo(f"  Host:        {conn.host}")
        typer.echo(f"  Port:        {conn.port}")
        typer.echo(f"  Database:    {conn.database}")
        typer.echo(f"  Username:    {conn.username}")
        typer.echo(f"  Encrypt:     {conn.encrypt}")
        typer.echo(f"  Trust Cert:  {conn.trust_server_certificate}")
    elif isinstance(conn, MongoDBConnection):
        typer.echo(f"  Host:        {conn.host}")
        typer.echo(f"  Port:        {conn.port}")
        typer.echo(f"  Database:    {conn.database}")
        typer.echo(f"  Username:    {conn.username or '(none)'}")
        typer.echo(f"  Auth Source: {conn.auth_source}")
        typer.echo(f"  TLS:         {conn.tls}")


@profile_app.command(name="rename", help="Rename an existing profile preserving its UUID.")
def rename_profile(
    old_name: str = typer.Argument(..., help="Current profile name."),
    new_name: str = typer.Argument(..., help="New profile name."),
) -> None:
    """Rename an existing profile preserving its UUID."""
    service = _get_service()
    try:
        service.rename_profile(old_name, new_name)
        typer.echo(f"Profile '{old_name}' renamed to '{new_name}'.")
    except (ProfileValidationError, ProfileError) as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(code=1)


@profile_app.command(name="remove", help="Remove a saved database profile.")
def remove_profile(
    name: str = typer.Argument(..., help="Name of the profile to remove."),
    yes: bool = typer.Option(False, "--yes", "-y", help="Confirm removal without prompting."),
) -> None:
    """Remove a saved database profile."""
    service = _get_service()
    try:
        # Check existence first so user is not prompted for non-existent profile
        service.get_profile(name)
        if not yes:
            confirmed = typer.confirm(f"Are you sure you want to remove profile '{name}'?")
            if not confirmed:
                typer.echo("Aborted.")
                raise typer.Exit(code=0)

        service.remove_profile(name)
        typer.echo(f"Profile '{name}' removed.")
    except ProfileError as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(code=1)


def _is_interactive() -> bool:
    """Check whether stdin is attached to an interactive terminal."""
    return sys.stdin.isatty()


@profile_app.command(name="test", help="Test database connection for a saved profile.")
def test_profile(
    name: str = typer.Argument(..., help="Name of the profile to test."),
) -> None:
    """Establish connection, run test query, and report result without exposing credentials."""
    workflow = _get_connection_workflow()
    try:
        profile = workflow.get_profile(name)
        conn = profile.connection

        password = None
        used_saved = False

        if conn.database_type != DatabaseType.SQLITE:
            # 1. Check for saved credential
            password = workflow.resolve_saved_password(profile.id)
            used_saved = password is not None

            # 2. If no saved password, prompt for one-time password
            if not used_saved:
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

        # 3. Test connection
        result = workflow.test_profile_connection(
            name,
            password=password,
            used_saved_password=used_saved,
        )

        typer.echo("Connection succeeded.")
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
        raise typer.Exit(code=1)
    except (ConnectionError, ProfileError) as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(code=1)


@password_app.command(name="set", help="Securely store a database password for a profile.")
def set_password(
    name: str = typer.Argument(..., help="Name of the profile."),
) -> None:
    """Prompt for and securely store a database password."""
    service = _get_service()
    try:
        # Check profile existence and type before asking for password
        profile = service.get_profile(name)
        if profile.connection.database_type == DatabaseType.SQLITE:
            typer.echo(f"Error: Profile '{name}' uses SQLite, which does not support passwords.", err=True)
            raise typer.Exit(code=1)

        password = typer.prompt(
            f"Enter database password for profile '{name}'",
            hide_input=True,
        )
        confirm = typer.prompt(
            "Confirm database password",
            hide_input=True,
        )
        if password != confirm:
            typer.echo("Error: Passwords do not match.", err=True)
            raise typer.Exit(code=1)

        service.set_profile_password(name, password)
        typer.echo(f"Password for profile '{name}' stored securely.")
    except (ProfileValidationError, ProfileError, CredentialError) as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(code=1)


@password_app.command(name="status", help="Check whether a saved password exists for a profile.")
def password_status(
    name: str = typer.Argument(..., help="Name of the profile."),
) -> None:
    """Report whether a password exists without retrieving or printing it."""
    service = _get_service()
    try:
        profile = service.get_profile(name)
        if profile.connection.database_type == DatabaseType.SQLITE:
            typer.echo(f"Profile '{name}': SQLite does not use passwords.")
            return

        has_pw = service.has_profile_password(name)
        if has_pw:
            typer.echo(f"Profile '{name}': Saved password is present.")
        else:
            typer.echo(f"Profile '{name}': No saved password.")
    except (ProfileValidationError, ProfileError, CredentialError) as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(code=1)


@password_app.command(name="remove", help="Remove the saved password for a profile.")
def remove_password(
    name: str = typer.Argument(..., help="Name of the profile."),
) -> None:
    """Remove the saved password for a profile."""
    service = _get_service()
    try:
        service.remove_profile_password(name)
        typer.echo(f"Password for profile '{name}' removed.")
    except (ProfileValidationError, ProfileError, CredentialError) as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(code=1)
