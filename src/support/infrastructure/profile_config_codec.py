"""Type-specific connection configuration serialization and deserialization."""

from __future__ import annotations

import json

from support.application.errors import ProfileStorageError
from support.domain.profiles import (
    ConnectionConfig,
    DatabaseType,
    MariaDBConnection,
    MongoDBConnection,
    MySQLConnection,
    PostgresConnection,
    ProfileValidationError,
    SQLiteConnection,
    SQLServerConnection,
)


def serialize_connection_config(connection: ConnectionConfig) -> tuple[str, str]:
    """Serialize connection configuration to (database_type, config_json)."""
    db_type = connection.database_type.value
    if isinstance(connection, PostgresConnection):
        data = {
            "database": connection.database,
            "username": connection.username,
            "ssl_mode": connection.ssl_mode,
            "host": connection.host,
            "port": connection.port,
        }
    elif isinstance(connection, MySQLConnection):
        data = {
            "database": connection.database,
            "username": connection.username,
            "host": connection.host,
            "port": connection.port,
            "ssl_mode": connection.ssl_mode,
        }
    elif isinstance(connection, MariaDBConnection):
        data = {
            "database": connection.database,
            "username": connection.username,
            "host": connection.host,
            "port": connection.port,
            "ssl_mode": connection.ssl_mode,
        }
    elif isinstance(connection, SQLiteConnection):
        data = {
            "database_path": connection.database_path,
        }
    elif isinstance(connection, SQLServerConnection):
        data = {
            "database": connection.database,
            "username": connection.username,
            "host": connection.host,
            "port": connection.port,
            "encrypt": connection.encrypt,
            "trust_server_certificate": connection.trust_server_certificate,
        }
    elif isinstance(connection, MongoDBConnection):
        data = {
            "database": connection.database,
            "uri": connection.uri,
            "username": connection.username,
        }
    else:
        raise ProfileStorageError(
            f"Unsupported connection configuration type: {type(connection).__name__}"
        )

    return db_type, json.dumps(data, sort_keys=True)


def deserialize_connection_config(database_type: str, config_json: str) -> ConnectionConfig:
    """Deserialize (database_type, config_json) into a typed ConnectionConfig."""
    try:
        data = json.loads(config_json)
        if not isinstance(data, dict):
            raise ProfileStorageError("Connection configuration JSON must be an object.")
    except json.JSONDecodeError as exc:
        raise ProfileStorageError(f"Malformed JSON in connection configuration: {exc}") from exc

    try:
        try:
            db_type = DatabaseType(database_type)
        except ValueError:
            raise ProfileStorageError(f"Unknown database type '{database_type}' in profile storage.")

        if db_type == DatabaseType.POSTGRESQL:
            return PostgresConnection(
                database=data["database"],
                username=data["username"],
                ssl_mode=data["ssl_mode"],
                host=data.get("host", "localhost"),
                port=data.get("port", 5432),
            )
        elif db_type == DatabaseType.MYSQL:
            return MySQLConnection(
                database=data["database"],
                username=data["username"],
                host=data.get("host", "localhost"),
                port=data.get("port", 3306),
                ssl_mode=data.get("ssl_mode", "preferred"),
            )
        elif db_type == DatabaseType.MARIADB:
            return MariaDBConnection(
                database=data["database"],
                username=data["username"],
                host=data.get("host", "localhost"),
                port=data.get("port", 3306),
                ssl_mode=data.get("ssl_mode", "prefer"),
            )
        elif db_type == DatabaseType.SQLITE:
            return SQLiteConnection(
                database_path=data["database_path"],
            )
        elif db_type == DatabaseType.SQLSERVER:
            return SQLServerConnection(
                database=data["database"],
                username=data["username"],
                host=data.get("host", "localhost"),
                port=data.get("port", 1433),
                encrypt=data.get("encrypt", True),
                trust_server_certificate=data.get("trust_server_certificate", False),
            )
        elif db_type == DatabaseType.MONGODB:
            if "uri" not in data or not isinstance(data.get("uri"), str):
                raise ProfileStorageError(
                    "Profile configuration for MongoDB is obsolete and must be recreated."
                )
            return MongoDBConnection(
                database=data.get("database", "admin"),
                uri=data["uri"],
                username=data.get("username"),
            )
        else:
            raise ProfileStorageError(f"Unsupported database type: '{database_type}'.")
    except KeyError as exc:
        raise ProfileStorageError(f"Missing required connection field: {exc}") from exc
    except (TypeError, ProfileValidationError) as exc:
        raise ProfileStorageError(f"Invalid connection configuration: {exc}") from exc
