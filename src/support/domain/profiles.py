"""Database profile domain models and validation rules."""

from dataclasses import dataclass, field, replace
from enum import StrEnum
import re
from typing import Union
from uuid import UUID, uuid4


class DatabaseType(StrEnum):
    """Supported database backend types."""

    POSTGRESQL = "postgresql"
    MYSQL = "mysql"
    MARIADB = "mariadb"
    SQLITE = "sqlite"
    SQLSERVER = "sqlserver"
    MONGODB = "mongodb"


POSTGRES_SUPPORTED_SSL_MODES: frozenset[str] = frozenset(
    {"disable", "allow", "prefer", "require", "verify-ca", "verify-full"}
)
# Backward-compatible alias for existing imports
SUPPORTED_SSL_MODES: frozenset[str] = POSTGRES_SUPPORTED_SSL_MODES

MYSQL_SUPPORTED_SSL_MODES: frozenset[str] = frozenset(
    {"disabled", "preferred", "required", "verify_ca", "verify_identity"}
)

MARIADB_SUPPORTED_SSL_MODES: frozenset[str] = frozenset(
    {"disabled", "prefer", "require", "verify-ca", "verify-full"}
)

PROFILE_NAME_PATTERN: re.Pattern[str] = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")


class ProfileValidationError(ValueError):
    """Raised when database profile or connection configuration is invalid."""


def _validate_non_empty_str(value: str, field_name: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ProfileValidationError(f"{field_name} is required and cannot be empty.")


def _validate_port(port: int) -> None:
    if isinstance(port, bool) or not isinstance(port, int):
        raise ProfileValidationError(
            f"Port must be an integer, got {type(port).__name__}."
        )
    if not (1 <= port <= 65535):
        raise ProfileValidationError(
            f"Port must be between 1 and 65535, got {port}."
        )


@dataclass(frozen=True)
class PostgresConnection:
    """PostgreSQL-specific connection configuration.

    Attributes:
        database: Target database name (required).
        username: Database login user (required).
        ssl_mode: PostgreSQL SSL mode (required).
        host: Database host name or IP address (default: 'localhost').
        port: Database port number between 1 and 65535 (default: 5432).
    """

    database: str
    username: str
    ssl_mode: str
    host: str = "localhost"
    port: int = 5432

    @property
    def database_type(self) -> DatabaseType:
        return DatabaseType.POSTGRESQL

    def __post_init__(self) -> None:
        _validate_non_empty_str(self.database, "Database name")
        _validate_non_empty_str(self.username, "Username")
        _validate_non_empty_str(self.host, "Host")
        _validate_port(self.port)

        if not isinstance(self.ssl_mode, str):
            raise ProfileValidationError(
                f"SSL mode must be a string, got {type(self.ssl_mode).__name__}."
            )
        if self.ssl_mode not in POSTGRES_SUPPORTED_SSL_MODES:
            raise ProfileValidationError(
                f"Unsupported SSL mode '{self.ssl_mode}'. "
                f"Supported modes are: {', '.join(sorted(POSTGRES_SUPPORTED_SSL_MODES))}."
            )


@dataclass(frozen=True)
class MySQLConnection:
    """MySQL-specific connection configuration.

    Attributes:
        database: Target database name (required).
        username: Database login user (required).
        host: Database host name or IP address (default: 'localhost').
        port: Database port number between 1 and 65535 (default: 3306).
        ssl_mode: MySQL SSL mode (default: 'preferred').
    """

    database: str
    username: str
    host: str = "localhost"
    port: int = 3306
    ssl_mode: str = "preferred"

    @property
    def database_type(self) -> DatabaseType:
        return DatabaseType.MYSQL

    def __post_init__(self) -> None:
        _validate_non_empty_str(self.database, "Database name")
        _validate_non_empty_str(self.username, "Username")
        _validate_non_empty_str(self.host, "Host")
        _validate_port(self.port)

        if not isinstance(self.ssl_mode, str):
            raise ProfileValidationError(
                f"SSL mode must be a string, got {type(self.ssl_mode).__name__}."
            )
        if self.ssl_mode not in MYSQL_SUPPORTED_SSL_MODES:
            raise ProfileValidationError(
                f"Unsupported SSL mode '{self.ssl_mode}'. "
                f"Supported modes are: {', '.join(sorted(MYSQL_SUPPORTED_SSL_MODES))}."
            )


@dataclass(frozen=True)
class MariaDBConnection:
    """MariaDB-specific connection configuration.

    Attributes:
        database: Target database name (required).
        username: Database login user (required).
        host: Database host name or IP address (default: 'localhost').
        port: Database port number between 1 and 65535 (default: 3306).
        ssl_mode: MariaDB SSL mode (default: 'prefer').
    """

    database: str
    username: str
    host: str = "localhost"
    port: int = 3306
    ssl_mode: str = "prefer"

    @property
    def database_type(self) -> DatabaseType:
        return DatabaseType.MARIADB

    def __post_init__(self) -> None:
        _validate_non_empty_str(self.database, "Database name")
        _validate_non_empty_str(self.username, "Username")
        _validate_non_empty_str(self.host, "Host")
        _validate_port(self.port)

        if not isinstance(self.ssl_mode, str):
            raise ProfileValidationError(
                f"SSL mode must be a string, got {type(self.ssl_mode).__name__}."
            )
        if self.ssl_mode not in MARIADB_SUPPORTED_SSL_MODES:
            raise ProfileValidationError(
                f"Unsupported SSL mode '{self.ssl_mode}'. "
                f"Supported modes are: {', '.join(sorted(MARIADB_SUPPORTED_SSL_MODES))}."
            )


@dataclass(frozen=True)
class SQLiteConnection:
    """SQLite-specific connection configuration for local or embedded databases.

    Attributes:
        database_path: File system path to the SQLite database file or ':memory:'.
    """

    database_path: str

    @property
    def database_type(self) -> DatabaseType:
        return DatabaseType.SQLITE

    def __post_init__(self) -> None:
        _validate_non_empty_str(self.database_path, "Database path")


@dataclass(frozen=True)
class SQLServerConnection:
    """Microsoft SQL Server connection configuration.

    Attributes:
        database: Target database name (required).
        username: Database login user (required).
        host: Database host name or IP address (default: 'localhost').
        port: Database port number between 1 and 65535 (default: 1433).
        encrypt: Whether to require encryption (default: True).
        trust_server_certificate: Whether to trust server certificate without validation (default: False).
    """

    database: str
    username: str
    host: str = "localhost"
    port: int = 1433
    encrypt: bool = True
    trust_server_certificate: bool = False

    @property
    def database_type(self) -> DatabaseType:
        return DatabaseType.SQLSERVER

    def __post_init__(self) -> None:
        _validate_non_empty_str(self.database, "Database name")
        _validate_non_empty_str(self.username, "Username")
        _validate_non_empty_str(self.host, "Host")
        _validate_port(self.port)

        if not isinstance(self.encrypt, bool):
            raise ProfileValidationError(
                f"encrypt must be a boolean, got {type(self.encrypt).__name__}."
            )
        if not isinstance(self.trust_server_certificate, bool):
            raise ProfileValidationError(
                f"trust_server_certificate must be a boolean, got {type(self.trust_server_certificate).__name__}."
            )


@dataclass(frozen=True)
class MongoDBConnection:
    """MongoDB connection configuration.

    Attributes:
        database: Target database name (required).
        username: Database login user (optional).
        host: Database host name or IP address (default: 'localhost').
        port: Database port number between 1 and 65535 (default: 27017).
        auth_source: Authentication database (default: 'admin').
        tls: Whether to use TLS encryption (default: False).
    """

    database: str
    username: str | None = None
    host: str = "localhost"
    port: int = 27017
    auth_source: str = "admin"
    tls: bool = False

    @property
    def database_type(self) -> DatabaseType:
        return DatabaseType.MONGODB

    def __post_init__(self) -> None:
        _validate_non_empty_str(self.database, "Database name")
        _validate_non_empty_str(self.host, "Host")
        _validate_port(self.port)
        _validate_non_empty_str(self.auth_source, "Auth source")

        if self.username is not None:
            if not isinstance(self.username, str) or not self.username.strip():
                raise ProfileValidationError("Username cannot be empty when provided.")

        if not isinstance(self.tls, bool):
            raise ProfileValidationError(
                f"tls must be a boolean, got {type(self.tls).__name__}."
            )


ConnectionConfig = Union[
    PostgresConnection,
    MySQLConnection,
    MariaDBConnection,
    SQLiteConnection,
    SQLServerConnection,
    MongoDBConnection,
]

SUPPORTED_CONNECTION_TYPES = (
    PostgresConnection,
    MySQLConnection,
    MariaDBConnection,
    SQLiteConnection,
    SQLServerConnection,
    MongoDBConnection,
)


@dataclass(frozen=True)
class DatabaseProfile:
    """Represents a saved database connection configuration and access identity.

    Attributes:
        name: Human-readable profile alias (1-64 chars: lowercase alphanumeric, hyphens, underscores).
        connection: Database connection configuration.
        id: Stable unique identifier (UUID v4), automatically generated if omitted.
    """

    name: str
    connection: ConnectionConfig
    id: UUID = field(default_factory=uuid4)

    @property
    def database_type(self) -> DatabaseType:
        """Convenience accessor for connection database type."""
        return self.connection.database_type

    def __post_init__(self) -> None:
        # Validate name
        if not isinstance(self.name, str) or not PROFILE_NAME_PATTERN.fullmatch(self.name):
            raise ProfileValidationError(
                f"Invalid profile name '{self.name}'. "
                "Profile names must be 1-64 characters long, start with a lowercase letter or number, "
                "and contain only lowercase letters, numbers, hyphens, and underscores."
            )

        # Validate connection
        if not isinstance(self.connection, SUPPORTED_CONNECTION_TYPES):
            valid_names = ", ".join(t.__name__ for t in SUPPORTED_CONNECTION_TYPES)
            raise ProfileValidationError(
                f"connection must be one of ({valid_names}), got {type(self.connection).__name__}."
            )

        # Validate ID
        if not isinstance(self.id, UUID):
            raise ProfileValidationError(
                f"Profile ID must be a UUID instance, got {type(self.id).__name__}."
            )

    def rename(self, new_name: str) -> "DatabaseProfile":
        """Return a new DatabaseProfile with updated name, preserving the original UUID and connection."""
        return replace(self, name=new_name)
