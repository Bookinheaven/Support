"""Infrastructure layer for Support."""

from support.infrastructure.config import get_database_path
from support.infrastructure.dispatcher import ConnectorDispatcher
from support.infrastructure.keyring_credentials import (
    APPROVED_BACKENDS,
    SUPPORT_KEYRING_SERVICE,
    KeyringCredentialStore,
)
from support.infrastructure.mysql_connector import (
    DEFAULT_MYSQL_TIMEOUT_SECONDS,
    MySQLCompatibleConnector,
)
from support.infrastructure.postgres import DEFAULT_CONNECT_TIMEOUT_SECONDS, PostgresConnector
from support.infrastructure.sqlite_connector import (
    DEFAULT_SQLITE_TIMEOUT_SECONDS,
    SQLiteConnector,
)
from support.infrastructure.sqlite_profiles import SQLiteProfileRepository

__all__ = [
    "APPROVED_BACKENDS",
    "ConnectorDispatcher",
    "DEFAULT_CONNECT_TIMEOUT_SECONDS",
    "DEFAULT_MYSQL_TIMEOUT_SECONDS",
    "DEFAULT_SQLITE_TIMEOUT_SECONDS",
    "KeyringCredentialStore",
    "MySQLCompatibleConnector",
    "PostgresConnector",
    "SUPPORT_KEYRING_SERVICE",
    "SQLiteConnector",
    "SQLiteProfileRepository",
    "get_database_path",
]
