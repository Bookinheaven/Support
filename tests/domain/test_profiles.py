"""Unit tests for the DatabaseProfile and PostgresConnection domain models."""

from dataclasses import FrozenInstanceError
from uuid import UUID, uuid4

import pytest

from support.domain.profiles import (
    DatabaseProfile,
    DatabaseType,
    MongoDBConnection,
    PostgresConnection,
    ProfileValidationError,
    POSTGRES_SUPPORTED_SSL_MODES,
)


def _sample_connection(**kwargs) -> PostgresConnection:
    """Helper to construct a valid PostgresConnection with defaults."""
    defaults = {
        "database": "myapp_db",
        "username": "myapp_user",
        "ssl_mode": "prefer",
        "host": "localhost",
        "port": 5432,
    }
    defaults.update(kwargs)
    return PostgresConnection(**defaults)


# 1. Valid PostgresConnection creation and defaults
def test_postgres_connection_defaults() -> None:
    conn = PostgresConnection(database="core_db", username="app_user", ssl_mode="require")
    assert conn.database == "core_db"
    assert conn.username == "app_user"
    assert conn.ssl_mode == "require"
    assert conn.host == "localhost"
    assert conn.port == 5432


def test_postgres_connection_custom_values() -> None:
    conn = PostgresConnection(
        host="db.internal.example.com",
        port=5433,
        database="analytics",
        username="readonly_user",
        ssl_mode="verify-full",
    )
    assert conn.host == "db.internal.example.com"
    assert conn.port == 5433
    assert conn.database == "analytics"
    assert conn.username == "readonly_user"
    assert conn.ssl_mode == "verify-full"


# 2. PostgresConnection port validation
@pytest.mark.parametrize("invalid_port", [0, -1, 65536, 70000, "5432", True, 5432.0])
def test_postgres_connection_invalid_ports(invalid_port) -> None:
    with pytest.raises(ProfileValidationError, match="Port must be"):
        _sample_connection(port=invalid_port)


# 3. PostgresConnection required fields validation
@pytest.mark.parametrize("empty_val", ["", "   "])
def test_postgres_connection_missing_required_fields(empty_val: str) -> None:
    with pytest.raises(ProfileValidationError, match="Database name is required"):
        _sample_connection(database=empty_val)

    with pytest.raises(ProfileValidationError, match="Username is required"):
        _sample_connection(username=empty_val)

    with pytest.raises(ProfileValidationError, match="Host is required"):
        _sample_connection(host=empty_val)


# 4. PostgresConnection SSL mode validation
@pytest.mark.parametrize("ssl_mode", list(POSTGRES_SUPPORTED_SSL_MODES))
def test_postgres_connection_supported_ssl_modes(ssl_mode: str) -> None:
    conn = _sample_connection(ssl_mode=ssl_mode)
    assert conn.ssl_mode == ssl_mode


@pytest.mark.parametrize("invalid_ssl", ["invalid", "none", "", "REQUIRED", 123])
def test_postgres_connection_unsupported_ssl_mode(invalid_ssl) -> None:
    with pytest.raises(ProfileValidationError, match="SSL mode"):
        _sample_connection(ssl_mode=invalid_ssl)


# 5. DatabaseProfile creation and UUID generation
def test_valid_profile_creation() -> None:
    conn = _sample_connection()
    profile = DatabaseProfile(name="local-dev", connection=conn)

    assert profile.name == "local-dev"
    assert profile.connection == conn
    assert isinstance(profile.id, UUID)


def test_profile_automatic_uuid_generation_is_unique() -> None:
    conn = _sample_connection()
    p1 = DatabaseProfile(name="dev-1", connection=conn)
    p2 = DatabaseProfile(name="dev-2", connection=conn)

    assert isinstance(p1.id, UUID)
    assert isinstance(p2.id, UUID)
    assert p1.id != p2.id


def test_profile_explicit_uuid_preserved() -> None:
    conn = _sample_connection()
    custom_id = uuid4()
    profile = DatabaseProfile(name="staging", connection=conn, id=custom_id)
    assert profile.id == custom_id


def test_profile_invalid_id() -> None:
    conn = _sample_connection()
    with pytest.raises(ProfileValidationError, match="Profile ID must be a UUID instance, got str"):
        DatabaseProfile(name="dev", connection=conn, id=str(uuid4()))  # type: ignore

    with pytest.raises(ProfileValidationError, match="Profile ID must be a UUID instance, got int"):
        DatabaseProfile(name="dev", connection=conn, id=12345)  # type: ignore


# 6. Profile renaming preserves identity
def test_profile_rename_preserves_uuid() -> None:
    conn = _sample_connection()
    original = DatabaseProfile(name="dev-initial", connection=conn)
    renamed = original.rename("dev-renamed")

    assert renamed.id == original.id
    assert renamed.name == "dev-renamed"
    assert renamed.connection == original.connection


def test_profile_rename_rejects_invalid_name() -> None:
    conn = _sample_connection()
    profile = DatabaseProfile(name="valid-name", connection=conn)

    with pytest.raises(ProfileValidationError, match="Invalid profile name"):
        profile.rename("INVALID_UPPERCASE")


# 7. Profile name validation rules
@pytest.mark.parametrize(
    "valid_name",
    [
        "a",
        "1",
        "local-dev",
        "staging_db",
        "prod-read-only-2",
        "pg_123_test",
        "a" * 64,
    ],
)
def test_valid_profile_names(valid_name: str) -> None:
    conn = _sample_connection()
    profile = DatabaseProfile(name=valid_name, connection=conn)
    assert profile.name == valid_name


@pytest.mark.parametrize(
    "invalid_name",
    [
        "",  # Empty
        "   ",  # Whitespace
        "-start-with-hyphen",
        "_start-with-underscore",
        "Uppercase",
        "localDev",
        "local dev",  # Space
        "local/dev",  # Slash
        "local:dev",  # Colon
        "local.dev",  # Dot
        "local@dev",  # At-sign
        "a" * 65,  # Exceeds 64 characters
    ],
)
def test_invalid_profile_names(invalid_name: str) -> None:
    conn = _sample_connection()
    with pytest.raises(ProfileValidationError, match="Invalid profile name"):
        DatabaseProfile(name=invalid_name, connection=conn)


# 8. Immutability checks
def test_models_are_frozen() -> None:
    conn = _sample_connection()
    profile = DatabaseProfile(name="frozen-profile", connection=conn)

    with pytest.raises(FrozenInstanceError):
        profile.name = "new-name"  # type: ignore

    with pytest.raises(FrozenInstanceError):
        conn.port = 9999  # type: ignore


# 9. Security boundary: models prohibit passwords
def test_models_do_not_accept_passwords() -> None:
    conn = _sample_connection()
    with pytest.raises(TypeError, match="unexpected keyword argument 'password'"):
        DatabaseProfile(name="test", connection=conn, password="secret")  # type: ignore

    with pytest.raises(TypeError, match="unexpected keyword argument 'password'"):
        PostgresConnection(database="db", username="user", password="secret")  # type: ignore


# 10. Multi-database connection models
def test_multi_database_connection_models_valid() -> None:
    from support.domain.profiles import (
        DatabaseType,
        MariaDBConnection,
        MongoDBConnection,
        MySQLConnection,
        SQLiteConnection,
        SQLServerConnection,
    )

    mysql = MySQLConnection(database="db", username="user")
    assert mysql.database_type == DatabaseType.MYSQL
    assert mysql.port == 3306

    maria = MariaDBConnection(database="db", username="user")
    assert maria.database_type == DatabaseType.MARIADB
    assert maria.port == 3306

    sqlite = SQLiteConnection(database_path="/data/app.db")
    assert sqlite.database_type == DatabaseType.SQLITE
    assert sqlite.database_path == "/data/app.db"

    sqlserver = SQLServerConnection(database="db", username="sa")
    assert sqlserver.database_type == DatabaseType.SQLSERVER
    assert sqlserver.port == 1433
    assert sqlserver.encrypt is True

    mongo = MongoDBConnection(database="db")
    assert mongo.database_type == DatabaseType.MONGODB
    assert mongo.port == 27017
    assert mongo.host == "localhost"
    assert mongo.username is None
    assert mongo.uri == "mongodb://localhost:27017"


def test_mongodb_connection_uri_and_validation() -> None:
    # 1. Valid standard URI
    conn1 = MongoDBConnection(database="testdb", uri="mongodb://127.0.0.1:27018/testdb", username="u1")
    assert conn1.host == "127.0.0.1"
    assert conn1.port == 27018
    assert conn1.database == "testdb"
    assert conn1.username == "u1"

    # 2. Valid SRV URI
    conn2 = MongoDBConnection(uri="mongodb+srv://cluster.example.mongodb.net/analytics?retryWrites=true")
    assert conn2.host == "cluster.example.mongodb.net"
    assert conn2.port is None
    assert conn2.database == "analytics"

    # 3. Reject embedded username
    with pytest.raises(ProfileValidationError, match="embedded credentials"):
        MongoDBConnection(uri="mongodb://admin@localhost:27017")

    # 4. Reject embedded username and password
    with pytest.raises(ProfileValidationError, match="embedded credentials"):
        MongoDBConnection(uri="mongodb://admin:pass123@localhost:27017/db")

    # 5. Reject SRV URI with explicit port
    with pytest.raises(ProfileValidationError, match="must not specify a port"):
        MongoDBConnection(uri="mongodb+srv://cluster.example.net:27017")

    # 6. Reject invalid scheme
    with pytest.raises(ProfileValidationError, match="Unsupported MongoDB URI scheme"):
        MongoDBConnection(uri="http://localhost:27017")

    # 7. Reject empty host
    with pytest.raises(ProfileValidationError, match="must include a host"):
        MongoDBConnection(uri="mongodb://")

    # 8. Reject empty username when provided
    with pytest.raises(ProfileValidationError, match="Username cannot be empty"):
        MongoDBConnection(username="   ")


def test_mongodb_target_database_versus_auth_source() -> None:
    # Scenario 1: URI contains /analytics?authSource=admin
    # Target database is analytics, authentication database is explicitly admin
    c1 = MongoDBConnection(uri="mongodb://localhost:27017/analytics?authSource=admin")
    assert c1.database == "analytics"
    assert c1.auth_source == "admin"

    # Scenario 2: URI contains /analytics but no authSource
    # Target database is analytics, authentication database defaults to path database (analytics)
    c2 = MongoDBConnection(uri="mongodb://localhost:27017/analytics")
    assert c2.database == "analytics"
    assert c2.auth_source == "analytics"

    # Scenario 3: URI contains no database path and no authSource
    # Target database is default admin, authentication database defaults to admin
    c3 = MongoDBConnection(uri="mongodb://localhost:27017")
    assert c3.database == "admin"
    assert c3.auth_source == "admin"

    # Scenario 4: database="analytics" configured separately while URI has no database path
    # Target database is analytics, authentication database defaults to admin (pathless URI specification)
    c4 = MongoDBConnection(database="analytics", uri="mongodb://localhost:27017")
    assert c4.database == "analytics"
    assert c4.auth_source == "admin"


def test_mongodb_rejects_secret_bearing_uri_options() -> None:
    # 1. tlsCertificateKeyFilePassword
    with pytest.raises(ProfileValidationError, match="Secret-bearing URI option 'tlsCertificateKeyFilePassword'"):
        MongoDBConnection(uri="mongodb://localhost:27017/?tlsCertificateKeyFilePassword=my_cert_pass")

    # 2. sslCertificateKeyFilePassword
    with pytest.raises(ProfileValidationError, match="Secret-bearing URI option 'sslCertificateKeyFilePassword'"):
        MongoDBConnection(uri="mongodb://localhost:27017/?sslCertificateKeyFilePassword=my_cert_pass")

    # 3. proxyPassword
    with pytest.raises(ProfileValidationError, match="Secret-bearing URI option 'proxyPassword'"):
        MongoDBConnection(uri="mongodb://localhost:27017/?proxyPassword=my_proxy_secret")

    # 4. authMechanismProperties
    with pytest.raises(ProfileValidationError, match="Secret-bearing URI option 'authMechanismProperties'"):
        MongoDBConnection(uri="mongodb://localhost:27017/?authMechanismProperties=AWS_SESSION_TOKEN:token123")

    # Non-secret options are preserved without errors
    allowed = MongoDBConnection(uri="mongodb://localhost:27017/analytics?replicaSet=rs0&tls=true&retryWrites=true&authSource=admin")
    assert allowed.database == "analytics"
    assert allowed.auth_source == "admin"


def test_database_profile_supports_all_connection_types_and_rename() -> None:
    from support.domain.profiles import DatabaseType, SQLiteConnection

    sqlite = SQLiteConnection(database_path="/data/app.db")
    profile = DatabaseProfile(name="sqlite-prof", connection=sqlite)
    assert profile.database_type == DatabaseType.SQLITE
    assert profile.connection == sqlite

    renamed = profile.rename("sqlite-renamed")
    assert renamed.id == profile.id
    assert renamed.name == "sqlite-renamed"
    assert renamed.connection == sqlite


def test_database_profile_rejects_invalid_connection_type() -> None:
    with pytest.raises(ProfileValidationError, match="connection must be one of"):
        DatabaseProfile(name="bad-conn", connection="not-a-connection")  # type: ignore
