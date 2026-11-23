"""Unit tests for MongoDBConnector using mocked PyMongo."""

from unittest.mock import MagicMock, patch
import pytest

pymongo = pytest.importorskip("pymongo")
from pymongo.errors import (
    AutoReconnect,
    ConfigurationError,
    ConnectionFailure,
    NetworkTimeout,
    OperationFailure,
    ServerSelectionTimeoutError,
)

from support.application.errors import (
    AuthenticationFailedError,
    ConnectionTimeoutError,
    GeneralConnectionError,
    HostUnreachableError,
)
from support.domain.profiles import MongoDBConnection
from support.infrastructure.mongodb import MongoDBConnector


def test_mongodb_connection_success_requires_ping_and_omits_build_info() -> None:
    connector = MongoDBConnector(timeout_seconds=10)
    conn = MongoDBConnection(database="analytics", uri="mongodb://localhost:27017/analytics")

    mock_client = MagicMock()
    mock_client.admin.command.return_value = {"ok": 1.0}

    with patch("pymongo.MongoClient", return_value=mock_client) as mock_mongo_cls:
        result = connector.test_connection(conn)

        mock_mongo_cls.assert_called_once_with(
            "mongodb://localhost:27017/analytics",
            serverSelectionTimeoutMS=10000,
            connectTimeoutMS=10000,
            socketTimeoutMS=10000,
            timeoutMS=10000,
        )
        # ping is required and is the decisive command
        mock_client.admin.command.assert_called_once_with("ping")
        # buildInfo is explicitly NOT called
        for call_args in mock_client.admin.command.call_args_list:
            assert "buildInfo" not in call_args.args

        # Neither target database nor collections were probed
        mock_client.__getitem__.assert_not_called()

        assert result.success is True
        assert result.database_type == "mongodb"
        # Configured target database is recorded as metadata without permission claims
        assert result.database == "analytics"
        assert result.host == "localhost"
        assert result.port == 27017
        assert result.server_version is None
        mock_client.close.assert_called_once()


def test_mongodb_connection_passes_credentials() -> None:
    connector = MongoDBConnector()
    conn = MongoDBConnection(
        database="shop",
        uri="mongodb://db.example.com:27017",
        username="app_user",
    )

    mock_client = MagicMock()
    mock_client.admin.command.return_value = {"ok": 1.0}

    with patch("pymongo.MongoClient", return_value=mock_client) as mock_mongo_cls:
        result = connector.test_connection(conn, password="secret_pass_123")

        mock_mongo_cls.assert_called_once_with(
            "mongodb://db.example.com:27017",
            serverSelectionTimeoutMS=10000,
            connectTimeoutMS=10000,
            socketTimeoutMS=10000,
            timeoutMS=10000,
            username="app_user",
            password="secret_pass_123",
        )
        assert result.success is True
        assert result.username == "app_user"
        mock_client.close.assert_called_once()


def test_mongodb_password_without_username_rejected() -> None:
    connector = MongoDBConnector()
    conn = MongoDBConnection(database="shop", uri="mongodb://localhost:27017")

    with pytest.raises(
        AuthenticationFailedError,
        match="Password was provided for MongoDB connection, but no username is configured",
    ):
        connector.test_connection(conn, password="orphan_password")


def test_mongodb_client_closed_on_failure() -> None:
    connector = MongoDBConnector()
    conn = MongoDBConnection(database="admin", uri="mongodb://localhost:27017")

    mock_client = MagicMock()
    mock_client.admin.command.side_effect = ServerSelectionTimeoutError("No servers found")

    with patch("pymongo.MongoClient", return_value=mock_client):
        with pytest.raises(HostUnreachableError):
            connector.test_connection(conn)

        mock_client.close.assert_called_once()


def test_mongodb_error_translation_authentication_failure() -> None:
    connector = MongoDBConnector()
    conn = MongoDBConnection(database="admin", uri="mongodb://localhost:27017", username="user1")

    mock_client = MagicMock()
    auth_err = OperationFailure("auth failed", code=18, details={"codeName": "AuthenticationFailed"})
    mock_client.admin.command.side_effect = auth_err

    with patch("pymongo.MongoClient", return_value=mock_client):
        with pytest.raises(
            AuthenticationFailedError,
            match="MongoDB authentication failed",
        ):
            connector.test_connection(conn, password="bad_password")


def test_mongodb_error_translation_authorization_failure() -> None:
    connector = MongoDBConnector()
    conn = MongoDBConnection(database="admin", uri="mongodb://localhost:27017", username="user1")

    mock_client = MagicMock()
    unauth_err = OperationFailure("not authorized on admin to execute command", code=13, details={"codeName": "Unauthorized"})
    mock_client.admin.command.side_effect = unauth_err

    with patch("pymongo.MongoClient", return_value=mock_client):
        with pytest.raises(
            GeneralConnectionError,
            match="Authenticated MongoDB user does not have permission to execute ping command",
        ):
            connector.test_connection(conn, password="pass")


def test_mongodb_error_translation_server_selection_timeout() -> None:
    connector = MongoDBConnector()
    conn = MongoDBConnection(database="admin", uri="mongodb://remote.example.com:27017")

    mock_client = MagicMock()
    mock_client.admin.command.side_effect = ServerSelectionTimeoutError("connection refused")

    with patch("pymongo.MongoClient", return_value=mock_client):
        with pytest.raises(
            HostUnreachableError,
            match=r"Could not connect to MongoDB server at 'remote\.example\.com'",
        ):
            connector.test_connection(conn)


def test_mongodb_error_translation_network_timeout() -> None:
    connector = MongoDBConnector(timeout_seconds=10)
    conn = MongoDBConnection(database="admin", uri="mongodb://remote.example.com:27017")

    mock_client = MagicMock()
    timeout_err = NetworkTimeout("timed out")
    # Verify PyMongo exception inheritance: NetworkTimeout is a subclass of AutoReconnect and ConnectionFailure
    assert isinstance(timeout_err, AutoReconnect)
    assert isinstance(timeout_err, ConnectionFailure)

    mock_client.admin.command.side_effect = timeout_err

    with patch("pymongo.MongoClient", return_value=mock_client):
        with pytest.raises(
            ConnectionTimeoutError,
            match=r"Connection timed out after 10 seconds connecting to MongoDB server at 'remote\.example\.com'",
        ):
            connector.test_connection(conn)


def test_mongodb_error_translation_general_auto_reconnect() -> None:
    connector = MongoDBConnector()
    conn = MongoDBConnection(database="admin", uri="mongodb://remote.example.com:27017")

    mock_client = MagicMock()
    # AutoReconnect that is NOT a NetworkTimeout or ServerSelectionTimeoutError
    reconnect_err = AutoReconnect("connection to 10.0.0.1:27017 closed")
    mock_client.admin.command.side_effect = reconnect_err

    with patch("pymongo.MongoClient", return_value=mock_client):
        with pytest.raises(
            HostUnreachableError,
            match=r"Could not connect to MongoDB server at 'remote\.example\.com'",
        ):
            connector.test_connection(conn)


def test_mongodb_error_translation_srv_dns_failure() -> None:
    connector = MongoDBConnector()
    conn = MongoDBConnection(database="admin", uri="mongodb+srv://cluster.example.com")

    mock_client = MagicMock()
    mock_client.admin.command.side_effect = ConfigurationError("ConfigurationError: No DNS SRV records found for cluster.example.com")

    with patch("pymongo.MongoClient", return_value=mock_client):
        with pytest.raises(
            HostUnreachableError,
            match=r"Could not resolve MongoDB SRV cluster address 'cluster\.example\.com'",
        ):
            connector.test_connection(conn)


def test_mongodb_error_translation_invalid_configuration() -> None:
    connector = MongoDBConnector()
    conn = MongoDBConnection(database="admin", uri="mongodb://localhost:27017")

    mock_client = MagicMock()
    mock_client.admin.command.side_effect = ConfigurationError("Invalid URI option 'foo'")

    with patch("pymongo.MongoClient", return_value=mock_client):
        with pytest.raises(
            GeneralConnectionError,
            match="Invalid MongoDB configuration or URI options",
        ):
            connector.test_connection(conn)


def test_mongodb_error_message_sanitizes_passwords_and_uris() -> None:
    connector = MongoDBConnector()
    raw_uri = "mongodb://secret_user:super_secret_pw@localhost:27017/admindb"
    # Create object bypassing __post_init__ validation just to test sanitization of driver-generated strings
    conn = object.__new__(MongoDBConnection)
    object.__setattr__(conn, "database", "admindb")
    object.__setattr__(conn, "uri", raw_uri)
    object.__setattr__(conn, "username", "secret_user")

    mock_client = MagicMock()
    leak_err = Exception(f"Failed to connect to {raw_uri} with password super_secret_pw")
    mock_client.admin.command.side_effect = leak_err

    with patch("pymongo.MongoClient", return_value=mock_client):
        with pytest.raises(GeneralConnectionError) as exc_info:
            connector.test_connection(conn, password="super_secret_pw")

        msg = str(exc_info.value)
        assert "super_secret_pw" not in msg
        assert "******" in msg
        assert raw_uri not in msg
