"""Domain layer for Support."""

from support.domain.profiles import (
    ConnectionConfig,
    DatabaseProfile,
    DatabaseType,
    MariaDBConnection,
    MongoDBConnection,
    MySQLConnection,
    PostgresConnection,
    ProfileValidationError,
    SQLiteConnection,
    SQLServerConnection,
    SUPPORTED_CONNECTION_TYPES,
    SUPPORTED_SSL_MODES,
    POSTGRES_SUPPORTED_SSL_MODES,
    MYSQL_SUPPORTED_SSL_MODES,
    MARIADB_SUPPORTED_SSL_MODES,
)

__all__ = [
    "ConnectionConfig",
    "DatabaseProfile",
    "DatabaseType",
    "MariaDBConnection",
    "MongoDBConnection",
    "MySQLConnection",
    "PostgresConnection",
    "ProfileValidationError",
    "SQLiteConnection",
    "SQLServerConnection",
    "SUPPORTED_CONNECTION_TYPES",
    "SUPPORTED_SSL_MODES",
    "POSTGRES_SUPPORTED_SSL_MODES",
    "MYSQL_SUPPORTED_SSL_MODES",
    "MARIADB_SUPPORTED_SSL_MODES",
]
