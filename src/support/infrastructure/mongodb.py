"""MongoDB connectivity and schema introspection implementation using official PyMongo."""

from __future__ import annotations

import re
from typing import Any

from support.application.errors import (
    AuthenticationFailedError,
    ConnectionTimeoutError,
    GeneralConnectionError,
    HostUnreachableError,
    ObjectNotFoundError,
    UnsupportedDatabaseDriverError,
)
from support.application.ports import ConnectionTestResult
from support.domain.profiles import MongoDBConnection
from support.domain.schema import (
    DatabaseSchema,
    IndexMetadata,
    TableMetadata,
    TableType,
    unquote_identifier,
)

DEFAULT_MONGODB_TIMEOUT_SECONDS: int = 10


def _sanitize_message(message: str, connection: MongoDBConnection, password: str | None = None) -> str:
    """Sanitize error messages to ensure secrets and full connection strings never leak."""
    clean = message
    if password and password in clean:
        clean = clean.replace(password, "******")
    if connection.uri in clean:
        clean = clean.replace(connection.uri, f"mongodb://{connection.host}")
    clean = re.sub(r"(?i)mongodb(?:\+srv)?://([^:]+):([^@]+)@", r"mongodb://\1:******@", clean)
    clean = re.sub(r"(?i)(password|pwd|authSource|tlsCertificateKeyFilePassword)=[^&;\s]+", r"\1=******", clean)
    return clean


def _parse_mongo_index(idx_name: str, idx_spec: dict[str, Any]) -> IndexMetadata:
    """Parse PyMongo index specification into domain IndexMetadata."""
    raw_keys = idx_spec.get("key", [])
    cols: list[str] = []
    orders: list[str] = []
    special_method: str | None = None

    for k in raw_keys:
        if isinstance(k, (list, tuple)) and len(k) >= 2:
            field_name, direction = k[0], k[1]
            cols.append(str(field_name))
            if direction == 1:
                orders.append("ASC")
            elif direction == -1:
                orders.append("DESC")
            else:
                dir_str = str(direction)
                orders.append(dir_str)
                special_method = dir_str
        elif isinstance(k, str):
            cols.append(k)
            orders.append("ASC")

    is_unique = idx_spec.get("unique", False)
    is_pk = (idx_name == "_id_")
    filter_expr = idx_spec.get("partialFilterExpression")
    filter_condition = str(filter_expr) if filter_expr is not None else None

    return IndexMetadata(
        name=idx_name,
        columns=tuple(cols),
        column_orders=tuple(orders),
        unique=is_unique,
        is_primary=is_pk,
        method=special_method,
        filter_condition=filter_condition,
    )


class MongoDBConnector:
    """Infrastructure connector for establishing, testing, and introspecting MongoDB databases."""

    def __init__(self, timeout_seconds: int = DEFAULT_MONGODB_TIMEOUT_SECONDS) -> None:
        self._timeout_seconds = timeout_seconds

    def _translate_error(
        self,
        exc: Exception,
        connection: MongoDBConnection,
        password: str | None = None,
    ) -> None:
        """Translate PyMongo exceptions into specific application connection errors."""
        try:
            from pymongo.errors import (
                AutoReconnect,
                ConfigurationError,
                ConnectionFailure,
                NetworkTimeout,
                OperationFailure,
                PyMongoError,
                ServerSelectionTimeoutError,
            )
        except ImportError:
            clean = _sanitize_message(str(exc), connection, password)
            raise GeneralConnectionError(f"MongoDB connection failed: {clean}") from exc

        if isinstance(exc, ServerSelectionTimeoutError):
            raise HostUnreachableError(
                f"Could not connect to MongoDB server at '{connection.host}'. "
                "Ensure the server is reachable and accepting connections."
            ) from exc

        if isinstance(exc, NetworkTimeout):
            raise ConnectionTimeoutError(
                f"Connection timed out after {self._timeout_seconds} seconds connecting to MongoDB server at '{connection.host}'."
            ) from exc

        if isinstance(exc, AutoReconnect):
            raise HostUnreachableError(
                f"Could not connect to MongoDB server at '{connection.host}'. "
                "Connection closed or reset."
            ) from exc

        if isinstance(exc, OperationFailure):
            clean = _sanitize_message(str(exc), connection, password)
            if exc.code == 18 or "authentication failed" in str(exc).lower():
                raise AuthenticationFailedError(
                    f"MongoDB authentication failed for user '{connection.username}'.",
                    used_saved_password=False,
                ) from exc
            if exc.code == 13 or "not authorized" in str(exc).lower() or "unauthorized" in str(exc).lower():
                raise GeneralConnectionError(
                    f"Authenticated MongoDB user does not have permission to execute ping command: {clean}"
                ) from exc
            raise GeneralConnectionError(f"MongoDB operation failed: {clean}") from exc

        if isinstance(exc, ConfigurationError):
            clean = _sanitize_message(str(exc), connection, password)
            if "srv" in clean.lower():
                raise HostUnreachableError(
                    f"Could not resolve MongoDB SRV cluster address '{connection.host}': {clean}"
                ) from exc
            raise GeneralConnectionError(f"Invalid MongoDB configuration or URI options: {clean}") from exc

        if isinstance(exc, ConnectionFailure):
            clean = _sanitize_message(str(exc), connection, password)
            raise GeneralConnectionError(
                f"Network failure connecting to MongoDB server at '{connection.host}': {clean}"
            ) from exc

        clean = _sanitize_message(str(exc), connection, password)
        raise GeneralConnectionError(f"MongoDB connection failed: {clean}") from exc

    def _get_client(
        self,
        connection: MongoDBConnection,
        password: str | None = None,
    ) -> Any:
        """Create and configure a PyMongo client."""
        try:
            from pymongo import MongoClient
        except ModuleNotFoundError as exc:
            if exc.name == "pymongo":
                raise UnsupportedDatabaseDriverError(
                    "MongoDB driver 'pymongo' is not installed. "
                    'Install it using: pip install "support-db[mongodb]"'
                ) from exc
            raise GeneralConnectionError(
                f"Failed to load MongoDB driver due to missing dependency or library: {exc}"
            ) from exc
        except ImportError as exc:
            raise GeneralConnectionError(
                f"Failed to load MongoDB driver due to import error: {exc}"
            ) from exc

        if password is not None and not connection.username:
            raise AuthenticationFailedError(
                "Password was provided for MongoDB connection, but no username is configured."
            )

        client_kwargs: dict[str, Any] = {
            "serverSelectionTimeoutMS": self._timeout_seconds * 1000,
            "connectTimeoutMS": self._timeout_seconds * 1000,
            "socketTimeoutMS": self._timeout_seconds * 1000,
            "timeoutMS": self._timeout_seconds * 1000,
        }

        if connection.username:
            client_kwargs["username"] = connection.username
            if password is not None:
                client_kwargs["password"] = password

        try:
            return MongoClient(connection.uri, **client_kwargs)
        except Exception as exc:
            self._translate_error(exc, connection, password=password)
            raise

    def test_connection(
        self,
        connection: MongoDBConnection,
        password: str | None = None,
    ) -> ConnectionTestResult:
        """Establish connection with configured URI and credentials, run ping, and return result."""
        client = None
        try:
            client = self._get_client(connection, password=password)
            client.admin.command("ping")

            return ConnectionTestResult(
                success=True,
                database_type="mongodb",
                database=connection.database,
                host=connection.host,
                port=connection.port,
                username=connection.username,
                server_version=None,
            )
        except (AuthenticationFailedError, GeneralConnectionError, HostUnreachableError, ConnectionTimeoutError):
            raise
        except Exception as exc:
            self._translate_error(exc, connection, password=password)
            raise
        finally:
            if client is not None:
                try:
                    client.close()
                except Exception:
                    pass

    def introspect_schema(
        self,
        connection: MongoDBConnection,
        password: str | None = None,
    ) -> DatabaseSchema:
        """Introspect MongoDB collection metadata and index definitions without reading documents."""
        client = None
        try:
            client = self._get_client(connection, password=password)
            db = client[connection.database]
            coll_info_list = list(db.list_collections())
            tables: list[TableMetadata] = []

            for c_info in coll_info_list:
                c_name = c_info.get("name", "")
                if not c_name or c_name.startswith("system."):
                    continue
                c_options = c_info.get("options", {})
                c_validator = c_options.get("validator")

                col_obj = db[c_name]
                try:
                    raw_indexes = col_obj.index_information()
                except Exception:
                    raw_indexes = {}

                indexes: list[IndexMetadata] = [
                    _parse_mongo_index(idx_name, idx_spec)
                    for idx_name, idx_spec in raw_indexes.items()
                ]

                tables.append(
                    TableMetadata(
                        name=c_name,
                        table_type=TableType.COLLECTION,
                        schema_name=None,
                        columns=(),
                        primary_key=None,
                        unique_constraints=(),
                        foreign_keys=(),
                        check_constraints=(),
                        indexes=tuple(indexes),
                        options=c_options,
                        validator=c_validator,
                    )
                )

            return DatabaseSchema(
                database_name=connection.database,
                engine="MongoDB",
                schemas=(),
                tables=tuple(tables),
                relationships=(),
            )
        except (AuthenticationFailedError, GeneralConnectionError, HostUnreachableError, ConnectionTimeoutError):
            raise
        except Exception as exc:
            self._translate_error(exc, connection, password=password)
            raise
        finally:
            if client is not None:
                try:
                    client.close()
                except Exception:
                    pass

    def introspect_table(
        self,
        connection: MongoDBConnection,
        table_name: str,
        schema: str | None = None,
        password: str | None = None,
    ) -> TableMetadata:
        """Introspect a single MongoDB collection without reading documents."""
        clean_name = unquote_identifier(table_name)
        client = None
        try:
            client = self._get_client(connection, password=password)
            db = client[connection.database]
            all_colls = list(db.list_collections())
            exact_match = [c for c in all_colls if c.get("name") == clean_name]
            if exact_match:
                c_info = exact_match[0]
            else:
                ci_match = [c for c in all_colls if c.get("name", "").lower() == clean_name.lower()]
                if not ci_match:
                    raise ObjectNotFoundError(f"Collection '{table_name}' not found in database '{connection.database}'.")
                c_info = ci_match[0]

            c_name = c_info["name"]
            c_options = c_info.get("options", {})
            c_validator = c_options.get("validator")

            col_obj = db[c_name]
            try:
                raw_indexes = col_obj.index_information()
            except Exception:
                raw_indexes = {}

            indexes = [
                _parse_mongo_index(idx_name, idx_spec)
                for idx_name, idx_spec in raw_indexes.items()
            ]

            return TableMetadata(
                name=c_name,
                table_type=TableType.COLLECTION,
                schema_name=None,
                columns=(),
                primary_key=None,
                unique_constraints=(),
                foreign_keys=(),
                check_constraints=(),
                indexes=tuple(indexes),
                options=c_options,
                validator=c_validator,
            )
        except (AuthenticationFailedError, GeneralConnectionError, HostUnreachableError, ConnectionTimeoutError, ObjectNotFoundError):
            raise
        except Exception as exc:
            self._translate_error(exc, connection, password=password)
            raise
        finally:
            if client is not None:
                try:
                    client.close()
                except Exception:
                    pass
