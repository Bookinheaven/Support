"""Unit tests for ConnectorDispatcher."""

from unittest.mock import MagicMock, patch
import tomllib
from pathlib import Path
import pytest

from support.application.errors import (
    GeneralConnectionError,
    UnsupportedDatabaseDriverError,
)
from support.application.ports import ConnectionTestResult
from support.domain.profiles import (
    MariaDBConnection,
    MongoDBConnection,
    MySQLConnection,
    PostgresConnection,
    SQLiteConnection,
    SQLServerConnection,
)
from support.infrastructure.dispatcher import ConnectorDispatcher


def test_dispatcher_routes_postgres_connection() -> None:
    mock_pg = MagicMock()
    expected_result = ConnectionTestResult(
        success=True,
        database_type="postgresql",
        database="testdb",
        username="postgres",
        host="localhost",
        port=5432,
        ssl_mode="prefer",
    )
    mock_pg.test_connection.return_value = expected_result

    dispatcher = ConnectorDispatcher(postgres_connector=mock_pg)
    conn = PostgresConnection(database="testdb", username="postgres", ssl_mode="prefer")

    result = dispatcher.test_connection(conn, password="secret")
    assert result == expected_result
    mock_pg.test_connection.assert_called_once_with(conn, password="secret")


def test_dispatcher_routes_sqlite_connection() -> None:
    mock_sqlite = MagicMock()
    expected_result = ConnectionTestResult(
        success=True,
        database_type="sqlite",
        database="test.db",
        server_version="3.45.0",
    )
    mock_sqlite.test_connection.return_value = expected_result

    dispatcher = ConnectorDispatcher(sqlite_connector=mock_sqlite)
    conn = SQLiteConnection(database_path="/path/test.db")

    result = dispatcher.test_connection(conn)
    assert result == expected_result
    mock_sqlite.test_connection.assert_called_once_with(conn, password=None)


def test_dispatcher_routes_mysql_and_mariadb_connection() -> None:
    mock_mysql = MagicMock()
    expected_result = ConnectionTestResult(
        success=True,
        database_type="mysql",
        database="shopdb",
        username="admin",
        host="localhost",
        port=3306,
        server_version="8.0.35",
    )
    mock_mysql.test_connection.return_value = expected_result

    dispatcher = ConnectorDispatcher(mysql_connector=mock_mysql)
    mysql_conn = MySQLConnection(database="shopdb", username="admin")
    result = dispatcher.test_connection(mysql_conn, password="pass")
    assert result == expected_result
    mock_mysql.test_connection.assert_called_once_with(mysql_conn, password="pass")

    mock_mysql.reset_mock()
    mariadb_conn = MariaDBConnection(database="shopdb", username="admin")
    dispatcher.test_connection(mariadb_conn, password="pass")
    mock_mysql.test_connection.assert_called_once_with(mariadb_conn, password="pass")


def test_dispatcher_routes_sqlserver_connection() -> None:
    mock_sqlserver = MagicMock()
    expected_result = ConnectionTestResult(
        success=True,
        database_type="sqlserver",
        database="proddb",
        username="sa",
        host="localhost",
        port=1433,
    )
    mock_sqlserver.test_connection.return_value = expected_result

    dispatcher = ConnectorDispatcher(sqlserver_connector=mock_sqlserver)
    sqlserver_conn = SQLServerConnection(database="proddb", username="sa")
    result = dispatcher.test_connection(sqlserver_conn, password="pass")
    assert result == expected_result
    mock_sqlserver.test_connection.assert_called_once_with(sqlserver_conn, password="pass")


def test_dispatcher_rejects_unsupported_database_type() -> None:
    dispatcher = ConnectorDispatcher()

    dummy_conn = MagicMock()
    with pytest.raises(
        UnsupportedDatabaseDriverError,
        match="Connection testing is not yet supported for database type 'MagicMock'",
    ):
        dispatcher.test_connection(dummy_conn)


def test_dispatcher_routes_mongodb_connection() -> None:
    mock_mongo = MagicMock()
    expected_result = ConnectionTestResult(
        success=True,
        database_type="mongodb",
        database="admin",
        host="localhost",
        port=27017,
    )
    mock_mongo.test_connection.return_value = expected_result

    dispatcher = ConnectorDispatcher(mongodb_connector=mock_mongo)
    mongo_conn = MongoDBConnection(database="admin", uri="mongodb://localhost:27017")
    result = dispatcher.test_connection(mongo_conn, password="pass")
    assert result == expected_result
    mock_mongo.test_connection.assert_called_once_with(mongo_conn, password="pass")


def test_dispatcher_postgres_missing_driver_raises_actionable_error() -> None:
    dispatcher = ConnectorDispatcher()
    conn = PostgresConnection(database="testdb", username="u", ssl_mode="prefer")

    orig_import = __import__

    def mock_import(name, *args, **kwargs):
        if "postgres" in name:
            raise ModuleNotFoundError("No module named 'psycopg'", name="psycopg")
        return orig_import(name, *args, **kwargs)

    with patch("builtins.__import__", side_effect=mock_import):
        with pytest.raises(
            UnsupportedDatabaseDriverError,
            match=r"PostgreSQL driver 'psycopg' is not installed\. Install it using: pip install \"support-db\[postgres\]\"",
        ):
            dispatcher.test_connection(conn)


def test_dispatcher_postgres_library_or_import_error_raises_general_connection_error() -> None:
    dispatcher = ConnectorDispatcher()
    conn = PostgresConnection(database="testdb", username="u", ssl_mode="prefer")

    orig_import = __import__

    def mock_import_lib(name, *args, **kwargs):
        if "postgres" in name:
            raise ModuleNotFoundError("libpq.so.5: cannot open shared object file", name="libpq")
        return orig_import(name, *args, **kwargs)

    with patch("builtins.__import__", side_effect=mock_import_lib):
        with pytest.raises(
            GeneralConnectionError,
            match=r"Failed to load PostgreSQL driver due to missing dependency or library",
        ):
            dispatcher.test_connection(conn)

    def mock_import_err(name, *args, **kwargs):
        if "postgres" in name:
            raise ImportError("symbol lookup error: undefined symbol")
        return orig_import(name, *args, **kwargs)

    with patch("builtins.__import__", side_effect=mock_import_err):
        with pytest.raises(
            GeneralConnectionError,
            match=r"Failed to load PostgreSQL driver due to import error",
        ):
            dispatcher.test_connection(conn)


def test_dispatcher_mysql_and_mariadb_missing_driver_raises_actionable_error() -> None:
    dispatcher = ConnectorDispatcher()
    mysql_conn = MySQLConnection(database="testdb", username="u")
    mariadb_conn = MariaDBConnection(database="testdb", username="u")

    orig_import = __import__

    def mock_import(name, *args, **kwargs):
        if "mysql" in name:
            raise ModuleNotFoundError("No module named 'pymysql'", name="pymysql")
        return orig_import(name, *args, **kwargs)

    with patch("builtins.__import__", side_effect=mock_import):
        with pytest.raises(
            UnsupportedDatabaseDriverError,
            match=r"MySQL driver 'PyMySQL' is not installed\. Install it using: pip install \"support-db\[mysql\]\"",
        ):
            dispatcher.test_connection(mysql_conn)

        with pytest.raises(
            UnsupportedDatabaseDriverError,
            match=r"MySQL driver 'PyMySQL' is not installed\. Install it using: pip install \"support-db\[mysql\]\"",
        ):
            dispatcher.test_connection(mariadb_conn)


def test_dispatcher_mysql_library_or_import_error_raises_general_connection_error() -> None:
    dispatcher = ConnectorDispatcher()
    conn = MySQLConnection(database="testdb", username="u")

    orig_import = __import__

    def mock_import_lib(name, *args, **kwargs):
        if "mysql" in name:
            raise ModuleNotFoundError("No module named 'cryptography'", name="cryptography")
        return orig_import(name, *args, **kwargs)

    with patch("builtins.__import__", side_effect=mock_import_lib):
        with pytest.raises(
            GeneralConnectionError,
            match=r"Failed to load MySQL driver due to missing dependency or library",
        ):
            dispatcher.test_connection(conn)

    def mock_import_err(name, *args, **kwargs):
        if "mysql" in name:
            raise ImportError("DLL initialization failed")
        return orig_import(name, *args, **kwargs)

    with patch("builtins.__import__", side_effect=mock_import_err):
        with pytest.raises(
            GeneralConnectionError,
            match=r"Failed to load MySQL driver due to import error",
        ):
            dispatcher.test_connection(conn)


def test_dispatcher_sqlserver_missing_driver_raises_actionable_error() -> None:
    dispatcher = ConnectorDispatcher()
    conn = SQLServerConnection(database="testdb", username="u")

    orig_import = __import__

    def mock_import(name, *args, **kwargs):
        if name in ("mssql_python", "mssql-python"):
            raise ModuleNotFoundError("No module named 'mssql_python'", name="mssql_python")
        return orig_import(name, *args, **kwargs)

    with patch("builtins.__import__", side_effect=mock_import):
        with pytest.raises(
            UnsupportedDatabaseDriverError,
            match=r"SQL Server driver 'mssql-python' is not installed\. Install it using: pip install \"support-db\[sqlserver\]\"",
        ):
            dispatcher.test_connection(conn)


def test_dispatcher_sqlserver_library_or_import_error_raises_general_connection_error() -> None:
    dispatcher = ConnectorDispatcher()
    conn = SQLServerConnection(database="testdb", username="u")

    orig_import = __import__

    def mock_import_lib(name, *args, **kwargs):
        if name in ("mssql_python", "mssql-python"):
            raise ModuleNotFoundError("No module named 'ddbc'", name="ddbc")
        return orig_import(name, *args, **kwargs)

    with patch("builtins.__import__", side_effect=mock_import_lib):
        with pytest.raises(
            GeneralConnectionError,
            match=r"Failed to load SQL Server driver due to missing dependency or library",
        ):
            dispatcher.test_connection(conn)

    def mock_import_err(name, *args, **kwargs):
        if name in ("mssql_python", "mssql-python"):
            raise ImportError("libltdl.so.7: cannot open shared object file")
        return orig_import(name, *args, **kwargs)

    with patch("builtins.__import__", side_effect=mock_import_err):
        with pytest.raises(
            GeneralConnectionError,
            match=r"Failed to load SQL Server driver due to import error",
        ):
            dispatcher.test_connection(conn)


def test_dispatcher_mongodb_missing_driver_raises_actionable_error() -> None:
    dispatcher = ConnectorDispatcher()
    conn = MongoDBConnection(database="testdb", uri="mongodb://localhost:27017")

    orig_import = __import__

    def mock_import(name, *args, **kwargs):
        if name == "pymongo" or "mongodb" in name:
            raise ModuleNotFoundError("No module named 'pymongo'", name="pymongo")
        return orig_import(name, *args, **kwargs)

    with patch("builtins.__import__", side_effect=mock_import):
        with pytest.raises(
            UnsupportedDatabaseDriverError,
            match=r"MongoDB driver 'pymongo' is not installed\. Install it using: pip install \"support-db\[mongodb\]\"",
        ):
            dispatcher.test_connection(conn)


def test_dispatcher_mongodb_library_or_import_error_raises_general_connection_error() -> None:
    dispatcher = ConnectorDispatcher()
    conn = MongoDBConnection(database="testdb", uri="mongodb://localhost:27017")

    orig_import = __import__

    def mock_import_lib(name, *args, **kwargs):
        if name == "pymongo" or "mongodb" in name:
            raise ModuleNotFoundError("No module named 'dnspython'", name="dnspython")
        return orig_import(name, *args, **kwargs)

    with patch("builtins.__import__", side_effect=mock_import_lib):
        with pytest.raises(
            GeneralConnectionError,
            match=r"Failed to load MongoDB driver due to missing dependency or library",
        ):
            dispatcher.test_connection(conn)

    def mock_import_err(name, *args, **kwargs):
        if name == "pymongo" or "mongodb" in name:
            raise ImportError("DLL initialization failed")
        return orig_import(name, *args, **kwargs)

    with patch("builtins.__import__", side_effect=mock_import_err):
        with pytest.raises(
            GeneralConnectionError,
            match=r"Failed to load MongoDB driver due to import error",
        ):
            dispatcher.test_connection(conn)


def test_dispatcher_sqlite_uses_stdlib_and_requires_no_external_driver(tmp_path: Path) -> None:
    import sqlite3

    db_path = tmp_path / "test.db"
    conn = sqlite3.connect(str(db_path))
    conn.execute("CREATE TABLE t (id INT)")
    conn.commit()
    conn.close()

    dispatcher = ConnectorDispatcher()
    sqlite_conn = SQLiteConnection(database_path=str(db_path))
    result = dispatcher.test_connection(sqlite_conn)

    assert result.success is True
    assert result.database_type == "sqlite"
    assert result.database == "test.db"


def test_pyproject_packaging_dependencies_and_extras() -> None:
    pyproject_path = Path(__file__).resolve().parents[2] / "pyproject.toml"
    with open(pyproject_path, "rb") as f:
        data = tomllib.load(f)

    project = data["project"]
    assert project["name"] == "support-db"
    assert project["scripts"]["support"] == "support.cli.main:cli"

    dependencies = project["dependencies"]
    optional_deps = project["optional-dependencies"]

    # Core dependencies must only contain typer, platformdirs, keyring, questionary, rich, and prompt-toolkit
    dep_names = [d.split(">=")[0].split("<")[0].split("[")[0].strip() for d in dependencies]
    assert sorted(dep_names) == ["keyring", "platformdirs", "prompt-toolkit", "questionary", "rich", "typer"]

    # Database drivers must not be in core dependencies
    assert "psycopg" not in dependencies
    assert "PyMySQL" not in dependencies
    assert "mssql-python" not in dependencies
    assert "pymongo" not in dependencies
    assert "sqlite3" not in dependencies

    # Extras must be declared
    assert set(optional_deps.keys()) == {"postgres", "mysql", "sqlserver", "mongodb", "all", "dev"}

    # No unimplemented extras
    assert "sqlite" not in optional_deps

    # Check extras contents
    assert any("psycopg" in req for req in optional_deps["postgres"])
    assert any("PyMySQL" in req for req in optional_deps["mysql"])
    assert any("mssql-python" in req for req in optional_deps["sqlserver"])
    assert any("pymongo" in req for req in optional_deps["mongodb"])

    # 'all' extra must combine all database drivers
    all_reqs = optional_deps["all"]
    assert any("psycopg" in req for req in all_reqs)
    assert any("PyMySQL" in req for req in all_reqs)
    assert any("mssql-python" in req for req in all_reqs)
    assert any("pymongo" in req for req in all_reqs)

