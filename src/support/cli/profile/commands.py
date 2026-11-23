"""Profile CRUD commands."""

import typer

from support.application.errors import CredentialError, ProfileError
from support.cli.composition import get_profile_service
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
    uri: str | None = typer.Option(
        None,
        "--uri",
        help="Connection URI (for mongodb, e.g. mongodb://localhost:27017 or mongodb+srv://cluster.mongodb.net/dbname).",
    ),
    auth_source: str = typer.Option("admin", "--auth-source", help="MongoDB auth source (default: admin)."),
    tls: bool = typer.Option(False, "--tls", help="Enable TLS (for mongodb)."),
    encrypt: bool = typer.Option(True, "--encrypt", help="Enable encryption (for sqlserver)."),
    trust_server_certificate: bool = typer.Option(
        False, "--trust-server-certificate", help="Trust server certificate (for sqlserver)."
    ),
) -> None:
    """Add a new database profile."""
    service = get_profile_service()
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
            resolved_uri = uri
            if not resolved_uri:
                resolved_port = port if port is not None else 27017
                resolved_db = database or "admin"
                query_params = []
                if tls:
                    query_params.append("tls=true")
                if auth_source and auth_source != "admin":
                    query_params.append(f"authSource={auth_source}")
                query_str = f"?{'&'.join(query_params)}" if query_params else ""
                resolved_uri = f"mongodb://{host}:{resolved_port}/{resolved_db}{query_str}"

            connection = MongoDBConnection(
                database=database or "admin",
                uri=resolved_uri,
                username=username,
            )

        profile = service.add_profile(name=name, connection=connection)
        typer.echo(f"Profile '{profile.name}' added successfully.")
    except (ProfileValidationError, ProfileError) as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(code=1)


def list_profiles() -> None:
    """List all saved database profiles."""
    service = get_profile_service()
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
            host_str = f"{conn.host}:{conn.port}" if conn.port is not None else conn.host
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


def show_profile(
    name: str = typer.Argument(..., help="Name of the profile to display."),
) -> None:
    """Display details of a saved database profile."""
    service = get_profile_service()
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
        typer.echo(f"  URI:         {conn.uri}")
        typer.echo(f"  Database:    {conn.database}")
        typer.echo(f"  Auth Source: {conn.auth_source}")
        typer.echo(f"  Username:    {conn.username or '(none)'}")


def rename_profile(
    old_name: str = typer.Argument(..., help="Current profile name."),
    new_name: str = typer.Argument(..., help="New profile name."),
) -> None:
    """Rename an existing profile preserving its UUID."""
    service = get_profile_service()
    try:
        service.rename_profile(old_name, new_name)
        typer.echo(f"Profile '{old_name}' renamed to '{new_name}'.")
    except (ProfileValidationError, ProfileError) as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(code=1)


def remove_profile(
    name: str = typer.Argument(..., help="Name of the profile to remove."),
    yes: bool = typer.Option(False, "--yes", "-y", help="Confirm removal without prompting."),
) -> None:
    """Remove a saved database profile."""
    service = get_profile_service()
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
    except (ProfileError, CredentialError) as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(code=1)
