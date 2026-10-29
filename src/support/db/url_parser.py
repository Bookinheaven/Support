"""Database URL parsing and profile normalization."""

import re
from pathlib import Path
from typing import Optional
from sqlalchemy.engine import make_url

from support.core.dialect import Dialect
from support.core.exceptions import SupportError, UnsupportedDialectError
from support.db.models import ConnectionProfile


def parse_driver_to_dialect(driver_name: str) -> Dialect:
    """Map a SQLAlchemy drivername (e.g., postgresql+psycopg, mysql, sqlserver) to Dialect."""
    if not driver_name:
        raise UnsupportedDialectError("empty driver name")

    base_driver = driver_name.split("+")[0].lower()

    if base_driver in ("postgres", "postgresql", "pgsql"):
        return Dialect.POSTGRES
    if base_driver in ("mysql", "mariadb"):
        return Dialect.MYSQL
    if base_driver in ("sqlite", "sqlite3"):
        return Dialect.SQLITE
    if base_driver in ("mssql", "sqlserver", "tsql"):
        return Dialect.MSSQL
    if base_driver in ("mongodb", "mongo"):
        return Dialect.MONGODB

    # Try general dialect parsing
    d = Dialect.from_string(base_driver)
    if d != Dialect.UNKNOWN:
        return d

    raise UnsupportedDialectError(driver_name)


def infer_profile_name(
    dialect: Dialect,
    database: str,
    existing_names: Optional[set[str]] = None,
) -> str:
    """Infer a clean, unique profile name from the database name or SQLite path."""
    names = {n.lower() for n in existing_names} if existing_names else set()

    if dialect == Dialect.SQLITE:
        if database in (":memory:", ""):
            base = "sqlite_memory"
        else:
            base = Path(database).stem or "sqlite_db"
    else:
        # Strip leading slashes or paths
        clean_db = database.strip("/").strip()
        base = clean_db if clean_db else f"{dialect.value}_db"

    # Sanitize base identifier
    clean_base = re.sub(r"[^A-Za-z0-9_-]", "_", base).strip("_")
    if not clean_base:
        clean_base = f"{dialect.value}_db"

    candidate = clean_base
    counter = 2
    while candidate.lower() in names:
        candidate = f"{clean_base}-{counter}"
        counter += 1

    return candidate


def parse_database_url(
    url_string: str,
    name_override: Optional[str] = None,
    existing_names: Optional[set[str]] = None,
) -> tuple[ConnectionProfile, Optional[str]]:
    """Parse a database connection URL into a ConnectionProfile and extracted password.

    Returns (profile, password).
    Raises SupportError on malformed or invalid URLs.
    """
    clean_url = url_string.strip()
    if not clean_url:
        raise SupportError("Connection URL cannot be empty.")

    try:
        parsed = make_url(clean_url)
    except Exception as e:
        raise SupportError(
            "Invalid database connection URL",
            details=f"Failed to parse URL syntax: {e}",
        )

    dialect = parse_driver_to_dialect(parsed.drivername)

    # Extract options / query parameters
    options = dict(parsed.query)

    # SQLite vs Client-Server
    if dialect == Dialect.SQLITE:
        db_path = parsed.database or ""
        if not db_path:
            raise SupportError(
                "Invalid SQLite URL",
                details="SQLite URL must specify a database path (e.g. sqlite:///path/to/db.sqlite or sqlite:///:memory:).",
            )
        profile_name = name_override or infer_profile_name(dialect, db_path, existing_names)
        profile = ConnectionProfile(
            name=profile_name,
            dialect=Dialect.SQLITE,
            database=db_path,
            options=options,
        )
        return profile, None

    # Client-server engines
    if dialect == Dialect.MONGODB:
        if "+srv" in parsed.drivername.lower():
            options["srv"] = True
        database_name = (parsed.database.strip("/") if parsed.database else "") or "admin"
    else:
        if not parsed.database:
            raise SupportError(
                "Invalid database URL",
                details="Database name is missing from the connection URL (e.g. postgresql://user:pass@host:port/database).",
            )
        database_name = parsed.database.strip("/")

    profile_name = name_override or infer_profile_name(dialect, database_name, existing_names)

    default_ports = {
        Dialect.POSTGRES: 5432,
        Dialect.MYSQL: 3306,
        Dialect.MSSQL: 1433,
        Dialect.MONGODB: 27017,
    }

    port = parsed.port or (None if options.get("srv") else default_ports.get(dialect))
    host = parsed.host or "127.0.0.1"
    username = parsed.username
    password = parsed.password

    profile = ConnectionProfile(
        name=profile_name,
        dialect=dialect,
        host=host,
        port=port,
        database=database_name,
        username=username,
        options=options,
    )

    return profile, password
