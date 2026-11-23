"""CLI tests for support profile test command."""

from pathlib import Path
from unittest.mock import MagicMock, patch
from typer.testing import CliRunner

from support.application.errors import (
    AuthenticationFailedError,
    ConnectionTimeoutError,
    CredentialError,
    CredentialRetrievalError,
    DatabaseNotFoundError,
    GeneralConnectionError,
    HostUnreachableError,
    SSLVerificationError,
)
from support.application.ports import ConnectionTestResult
from support.cli.main import app

runner = CliRunner()


def _add_test_profile(env: dict[str, str], name: str = "dev-db") -> None:
    res = runner.invoke(
        app,
        ["profile", "add", "--name", name, "--database", "myapp", "--username", "postgres", "--ssl-mode", "prefer"],
        env=env,
    )
    assert res.exit_code == 0


def test_profile_test_with_saved_password_success(tmp_path: Path) -> None:
    env = {"SUPPORT_HOME": str(tmp_path)}
    _add_test_profile(env, "prod-db")

    # Set saved password
    runner.invoke(app, ["profile", "password", "set", "prod-db"], input="secret_pass\nsecret_pass\n", env=env)

    mock_result = ConnectionTestResult(
        success=True,
        database_type="postgresql",
        database="myapp",
        username="postgres",
        host="localhost",
        port=5432,
        ssl_mode="prefer",
        server_version="16.2",
    )

    with patch("support.infrastructure.dispatcher.ConnectorDispatcher.test_connection", return_value=mock_result) as mock_test:
        # Should connect using saved password WITHOUT prompting
        res = runner.invoke(app, ["profile", "test", "prod-db"], env=env)
        assert res.exit_code == 0
        assert "Connection succeeded." in res.output
        assert "Database:    myapp" in res.output
        assert "User:        postgres" in res.output
        assert "Host:        localhost:5432" in res.output
        assert "Server:      PostgreSQL 16.2" in res.output
        # CRITICAL: secret must not appear in output
        assert "secret_pass" not in res.output
        mock_test.assert_called_once()
        assert mock_test.call_args.kwargs["password"] == "secret_pass"


def test_profile_test_with_one_time_password_prompt(tmp_path: Path) -> None:
    env = {"SUPPORT_HOME": str(tmp_path)}
    _add_test_profile(env, "otp-db")

    mock_result = ConnectionTestResult(
        success=True,
        database_type="postgresql",
        database="myapp",
        username="postgres",
        host="localhost",
        port=5432,
        ssl_mode="prefer",
    )

    one_time_pw = "one_time_secret_value"
    with patch("support.cli.profile.connection._is_interactive", return_value=True), patch(
        "support.infrastructure.dispatcher.ConnectorDispatcher.test_connection", return_value=mock_result
    ) as mock_test:
        # Interactive prompt for password
        res = runner.invoke(app, ["profile", "test", "otp-db"], input=f"{one_time_pw}\n", env=env)
        assert res.exit_code == 0
        assert "Connection succeeded." in res.output
        # Password must NOT appear in output
        assert one_time_pw not in res.output
        mock_test.assert_called_once()
        assert mock_test.call_args.kwargs["password"] == one_time_pw

    # Verify one-time password was NOT saved in credential store!
    status_res = runner.invoke(app, ["profile", "password", "status", "otp-db"], env=env)
    assert "No saved password." in status_res.output


def test_profile_test_non_interactive_without_saved_password_fails(tmp_path: Path) -> None:
    env = {"SUPPORT_HOME": str(tmp_path)}
    _add_test_profile(env, "headless-db")

    with patch("support.cli.profile.connection._is_interactive", return_value=False):
        res = runner.invoke(app, ["profile", "test", "headless-db"], env=env)
        assert res.exit_code == 1
        assert "Error: No saved password for profile 'headless-db' and non-interactive input is not supported." in res.output
        assert "support profile password set headless-db" in res.output


def test_profile_test_auth_failure_with_saved_password_suggests_update(tmp_path: Path) -> None:
    env = {"SUPPORT_HOME": str(tmp_path)}
    _add_test_profile(env, "bad-saved-auth")
    runner.invoke(app, ["profile", "password", "set", "bad-saved-auth"], input="old_pass\nold_pass\n", env=env)

    with patch(
        "support.infrastructure.dispatcher.ConnectorDispatcher.test_connection",
        side_effect=AuthenticationFailedError("Password authentication failed.", used_saved_password=True),
    ):
        res = runner.invoke(app, ["profile", "test", "bad-saved-auth"], env=env)
        assert res.exit_code == 1
        assert "Password authentication failed." in res.output
        assert "Update the saved password with: support profile password set bad-saved-auth" in res.output
        assert "old_pass" not in res.output


def test_profile_test_connection_failures_output_clear_messages(tmp_path: Path) -> None:
    env = {"SUPPORT_HOME": str(tmp_path)}
    _add_test_profile(env, "err-db")
    runner.invoke(app, ["profile", "password", "set", "err-db"], input="pass\npass\n", env=env)

    # 1. Connection refused / Host unreachable
    with patch(
        "support.infrastructure.dispatcher.ConnectorDispatcher.test_connection",
        side_effect=HostUnreachableError("Connection refused by 'localhost:5432'."),
    ):
        res = runner.invoke(app, ["profile", "test", "err-db"], env=env)
        assert res.exit_code == 1
        assert "Error: Connection refused by 'localhost:5432'." in res.output
        assert "Traceback" not in res.output

    # 2. Connection timeout
    with patch(
        "support.infrastructure.dispatcher.ConnectorDispatcher.test_connection",
        side_effect=ConnectionTimeoutError("Connection timed out after 10 seconds."),
    ):
        res = runner.invoke(app, ["profile", "test", "err-db"], env=env)
        assert res.exit_code == 1
        assert "Error: Connection timed out after 10 seconds." in res.output
        assert "Traceback" not in res.output

    # 3. Database not found
    with patch(
        "support.infrastructure.dispatcher.ConnectorDispatcher.test_connection",
        side_effect=DatabaseNotFoundError("Database 'myapp' does not exist."),
    ):
        res = runner.invoke(app, ["profile", "test", "err-db"], env=env)
        assert res.exit_code == 1
        assert "Error: Database 'myapp' does not exist." in res.output

    # 4. SSL verification error
    with patch(
        "support.infrastructure.dispatcher.ConnectorDispatcher.test_connection",
        side_effect=SSLVerificationError("SSL/TLS verification failed."),
    ):
        res = runner.invoke(app, ["profile", "test", "err-db"], env=env)
        assert res.exit_code == 1
        assert "Error: SSL/TLS verification failed." in res.output


def test_profile_test_sqlite_file_success(tmp_path: Path) -> None:
    import sqlite3

    env = {"SUPPORT_HOME": str(tmp_path)}
    db_file = tmp_path / "test.db"
    with sqlite3.connect(db_file) as conn:
        conn.execute("CREATE TABLE users (id INTEGER PRIMARY KEY, name TEXT);")

    res_add = runner.invoke(
        app,
        ["profile", "add", "--type", "sqlite", "--name", "my-sqlite", "--database-path", str(db_file)],
        env=env,
    )
    assert res_add.exit_code == 0

    # Test connection - must succeed without prompting for password
    res = runner.invoke(app, ["profile", "test", "my-sqlite"], env=env)
    assert res.exit_code == 0
    assert "Connection succeeded." in res.output
    assert "Type:        sqlite" in res.output
    assert f"Database:    {db_file.name}" in res.output
    assert "Server:      SQLite " in res.output


def test_profile_test_sqlite_memory_success(tmp_path: Path) -> None:
    env = {"SUPPORT_HOME": str(tmp_path)}
    res_add = runner.invoke(
        app,
        ["profile", "add", "--type", "sqlite", "--name", "mem-sqlite", "--database-path", ":memory:"],
        env=env,
    )
    assert res_add.exit_code == 0

    res = runner.invoke(app, ["profile", "test", "mem-sqlite"], env=env)
    assert res.exit_code == 0
    assert "Connection succeeded." in res.output
    assert "Type:        sqlite" in res.output
    assert "Database:    :memory:" in res.output
    assert "Server:      SQLite " in res.output


def test_profile_test_sqlite_missing_file_fails_cleanly(tmp_path: Path) -> None:
    env = {"SUPPORT_HOME": str(tmp_path)}
    missing_file = tmp_path / "nonexistent.db"

    res_add = runner.invoke(
        app,
        ["profile", "add", "--type", "sqlite", "--name", "missing-sqlite", "--database-path", str(missing_file)],
        env=env,
    )
    assert res_add.exit_code == 0

    res = runner.invoke(app, ["profile", "test", "missing-sqlite"], env=env)
    assert res.exit_code == 1
    assert "Error: SQLite database file not found:" in res.output
    # Crucial: test must not create the missing file
    assert not missing_file.exists()


def test_profile_test_sqlite_directory_fails(tmp_path: Path) -> None:
    env = {"SUPPORT_HOME": str(tmp_path)}
    res_add = runner.invoke(
        app,
        ["profile", "add", "--type", "sqlite", "--name", "dir-sqlite", "--database-path", str(tmp_path)],
        env=env,
    )
    assert res_add.exit_code == 0

    res = runner.invoke(app, ["profile", "test", "dir-sqlite"], env=env)
    assert res.exit_code == 1
    assert "Error: Provided path is a directory, not an SQLite database file:" in res.output


def test_profile_test_sqlite_corrupt_file_fails(tmp_path: Path) -> None:
    env = {"SUPPORT_HOME": str(tmp_path)}
    corrupt_file = tmp_path / "corrupt.db"
    corrupt_file.write_bytes(b"This is completely invalid sqlite data header.")

    res_add = runner.invoke(
        app,
        ["profile", "add", "--type", "sqlite", "--name", "bad-sqlite", "--database-path", str(corrupt_file)],
        env=env,
    )
    assert res_add.exit_code == 0

    res = runner.invoke(app, ["profile", "test", "bad-sqlite"], env=env)
    assert res.exit_code == 1
    assert "Error: Invalid or malformed SQLite database:" in res.output


def test_profile_test_sqlite_zero_byte_file_fails(tmp_path: Path) -> None:
    env = {"SUPPORT_HOME": str(tmp_path)}
    zero_file = tmp_path / "zero.db"
    zero_file.touch()

    res_add = runner.invoke(
        app,
        ["profile", "add", "--type", "sqlite", "--name", "zero-sqlite", "--database-path", str(zero_file)],
        env=env,
    )
    assert res_add.exit_code == 0

    res = runner.invoke(app, ["profile", "test", "zero-sqlite"], env=env)
    assert res.exit_code == 1
    assert "Error: Invalid or malformed SQLite database:" in res.output
    assert "empty file (0 bytes)" in res.output


def test_profile_test_mysql_with_saved_password_success(tmp_path: Path) -> None:
    env = {"SUPPORT_HOME": str(tmp_path)}
    res_add = runner.invoke(
        app,
        ["profile", "add", "--type", "mysql", "--name", "prod-mysql", "--database", "shop", "--username", "admin"],
        env=env,
    )
    assert res_add.exit_code == 0

    # Save password
    runner.invoke(app, ["profile", "password", "set", "prod-mysql"], input="mypass\nmypass\n", env=env)

    mock_result = ConnectionTestResult(
        success=True,
        database_type="mysql",
        database="shop",
        username="admin",
        host="localhost",
        port=3306,
        ssl_mode="preferred",
        server_version="8.0.35",
    )

    with patch(
        "support.infrastructure.dispatcher.ConnectorDispatcher.test_connection",
        return_value=mock_result,
    ) as mock_test:
        res = runner.invoke(app, ["profile", "test", "prod-mysql"], env=env)
        assert res.exit_code == 0
        assert "Connection succeeded." in res.output
        assert "Type:        mysql" in res.output
        assert "Database:    shop" in res.output
        assert "User:        admin" in res.output
        assert "Server:      MySQL 8.0.35" in res.output
        assert "mypass" not in res.output
        mock_test.assert_called_once()
        assert mock_test.call_args.kwargs["password"] == "mypass"


def test_profile_test_mariadb_with_saved_password_success(tmp_path: Path) -> None:
    env = {"SUPPORT_HOME": str(tmp_path)}
    res_add = runner.invoke(
        app,
        ["profile", "add", "--type", "mariadb", "--name", "prod-maria", "--database", "analytics", "--username", "root"],
        env=env,
    )
    assert res_add.exit_code == 0

    runner.invoke(app, ["profile", "password", "set", "prod-maria"], input="mariapass\nmariapass\n", env=env)

    mock_result = ConnectionTestResult(
        success=True,
        database_type="mariadb",
        database="analytics",
        username="root",
        host="localhost",
        port=3306,
        ssl_mode="prefer",
        server_version="10.11.6-MariaDB",
    )

    with patch(
        "support.infrastructure.dispatcher.ConnectorDispatcher.test_connection",
        return_value=mock_result,
    ) as mock_test:
        res = runner.invoke(app, ["profile", "test", "prod-maria"], env=env)
        assert res.exit_code == 0
        assert "Connection succeeded." in res.output
        assert "Type:        mariadb" in res.output
        assert "Database:    analytics" in res.output
        assert "Server:      MariaDB 10.11.6-MariaDB" in res.output
        assert "mariapass" not in res.output
        mock_test.assert_called_once()
        assert mock_test.call_args.kwargs["password"] == "mariapass"


def test_profile_test_sqlserver_with_saved_password_success(tmp_path: Path) -> None:
    env = {"SUPPORT_HOME": str(tmp_path)}
    res_add = runner.invoke(
        app,
        [
            "profile",
            "add",
            "--type",
            "sqlserver",
            "--name",
            "prod-sqlserver",
            "--database",
            "sales",
            "--username",
            "sa",
        ],
        env=env,
    )
    assert res_add.exit_code == 0

    runner.invoke(app, ["profile", "password", "set", "prod-sqlserver"], input="sqlpass\nsqlpass\n", env=env)

    mock_result = ConnectionTestResult(
        success=True,
        database_type="sqlserver",
        database="sales",
        username="sa",
        host="localhost",
        port=1433,
    )

    with patch(
        "support.infrastructure.dispatcher.ConnectorDispatcher.test_connection",
        return_value=mock_result,
    ) as mock_test:
        res = runner.invoke(app, ["profile", "test", "prod-sqlserver"], env=env)
        assert res.exit_code == 0
        assert "Connection succeeded." in res.output
        assert "Type:        sqlserver" in res.output
        assert "Database:    sales" in res.output
        assert "User:        sa" in res.output
        assert "Host:        localhost:1433" in res.output
        assert "sqlpass" not in res.output
        mock_test.assert_called_once()
        assert mock_test.call_args.kwargs["password"] == "sqlpass"


def test_profile_test_sqlserver_database_access_denied_no_password_remediation(tmp_path: Path) -> None:
    env = {"SUPPORT_HOME": str(tmp_path)}
    runner.invoke(
        app,
        [
            "profile",
            "add",
            "--type",
            "sqlserver",
            "--name",
            "restricted-sql",
            "--database",
            "secret_db",
            "--username",
            "app_user",
        ],
        env=env,
    )
    runner.invoke(app, ["profile", "password", "set", "restricted-sql"], input="pass123\npass123\n", env=env)

    with patch(
        "support.infrastructure.dispatcher.ConnectorDispatcher.test_connection",
        side_effect=GeneralConnectionError(
            "The requested database could not be opened. Verify that it exists and that the login has permission to access it."
        ),
    ):
        res = runner.invoke(app, ["profile", "test", "restricted-sql"], env=env)
        assert res.exit_code == 1
        assert "Error: The requested database could not be opened." in res.output
        assert "Verify that it exists and that the login has permission to access it." in res.output
        # Must NOT suggest updating or changing password
        assert "support profile password set" not in res.output
        assert "Next step: Update the saved password" not in res.output


def test_profile_test_mongodb_with_saved_password_success(tmp_path: Path) -> None:
    env = {"SUPPORT_HOME": str(tmp_path)}
    runner.invoke(
        app,
        [
            "profile",
            "add",
            "--type",
            "mongodb",
            "--name",
            "prod-mongo",
            "--uri",
            "mongodb://localhost:27017/analytics",
            "--username",
            "mongouser",
        ],
        env=env,
    )
    runner.invoke(app, ["profile", "password", "set", "prod-mongo"], input="mongopass\nmongopass\n", env=env)

    mock_result = ConnectionTestResult(
        success=True,
        database_type="mongodb",
        database="analytics",
        username="mongouser",
        host="localhost",
        port=27017,
        server_version=None,
    )

    with patch(
        "support.infrastructure.dispatcher.ConnectorDispatcher.test_connection",
        return_value=mock_result,
    ) as mock_test:
        res = runner.invoke(app, ["profile", "test", "prod-mongo"], env=env)
        assert res.exit_code == 0
        assert "Connection succeeded." in res.output
        assert "Type:        mongodb" in res.output
        assert "Database:    analytics" in res.output
        assert "User:        mongouser" in res.output
        assert "Host:        localhost:27017" in res.output
        assert "Server:" not in res.output
        assert "mongopass" not in res.output
        mock_test.assert_called_once()
        assert mock_test.call_args.kwargs["password"] == "mongopass"


def test_profile_test_mongodb_one_time_password(tmp_path: Path) -> None:
    env = {"SUPPORT_HOME": str(tmp_path)}
    runner.invoke(
        app,
        [
            "profile",
            "add",
            "--type",
            "mongodb",
            "--name",
            "otp-mongo",
            "--uri",
            "mongodb://localhost:27017/testdb",
            "--username",
            "app_user",
        ],
        env=env,
    )

    mock_result = ConnectionTestResult(
        success=True,
        database_type="mongodb",
        database="testdb",
        username="app_user",
        host="localhost",
        port=27017,
    )

    one_time_pw = "temp_mongo_pass_456"
    with patch("support.cli.profile.connection._is_interactive", return_value=True), patch(
        "support.infrastructure.dispatcher.ConnectorDispatcher.test_connection", return_value=mock_result
    ) as mock_test:
        res = runner.invoke(app, ["profile", "test", "otp-mongo"], input=f"{one_time_pw}\n", env=env)
        assert res.exit_code == 0
        assert "Connection succeeded." in res.output
        assert one_time_pw not in res.output
        mock_test.assert_called_once()
        assert mock_test.call_args.kwargs["password"] == one_time_pw

    # Password was not saved
    status_res = runner.invoke(app, ["profile", "password", "status", "otp-mongo"], env=env)
    assert "No saved password." in status_res.output


def test_profile_test_mongodb_unauthenticated_non_interactive_success(tmp_path: Path) -> None:
    env = {"SUPPORT_HOME": str(tmp_path)}
    runner.invoke(
        app,
        [
            "profile",
            "add",
            "--type",
            "mongodb",
            "--name",
            "unauth-mongo",
            "--uri",
            "mongodb://localhost:27017/analytics",
        ],
        env=env,
    )

    mock_result = ConnectionTestResult(
        success=True,
        database_type="mongodb",
        database="analytics",
        host="localhost",
        port=27017,
    )

    # In a non-interactive environment, unauthenticated MongoDB must NOT prompt or fail for missing password
    with patch("support.cli.profile.connection._is_interactive", return_value=False), patch(
        "support.infrastructure.dispatcher.ConnectorDispatcher.test_connection", return_value=mock_result
    ) as mock_test:
        res = runner.invoke(app, ["profile", "test", "unauth-mongo"], env=env)
        assert res.exit_code == 0
        assert "Connection succeeded." in res.output
        assert "Type:        mongodb" in res.output
        assert "Database:    analytics" in res.output
        # Verify no credentials were passed
        mock_test.assert_called_once()
        assert mock_test.call_args.kwargs["password"] is None


def test_profile_test_keyring_retrieval_failure_non_interactive_fails_cleanly(tmp_path: Path) -> None:
    env = {"SUPPORT_HOME": str(tmp_path)}
    _add_test_profile(env, "keyring-fail-headless")

    with patch(
        "support.application.connection.ConnectionTestWorkflow.resolve_saved_password",
        side_effect=CredentialRetrievalError("Secure store locked or inaccessible"),
    ), patch("support.cli.profile.connection._is_interactive", return_value=False):
        res = runner.invoke(app, ["profile", "test", "keyring-fail-headless"], env=env)
        assert res.exit_code == 1
        assert "Error: Failed to retrieve saved password for profile 'keyring-fail-headless'" in res.output
        assert "Secure store locked or inaccessible" in res.output
        # Must not produce an unhandled Python traceback
        assert "Traceback" not in res.output


def test_profile_test_keyring_retrieval_failure_interactive_falls_back_to_prompt(tmp_path: Path) -> None:
    env = {"SUPPORT_HOME": str(tmp_path)}
    _add_test_profile(env, "keyring-fail-interactive")

    mock_result = ConnectionTestResult(
        success=True,
        database_type="postgresql",
        database="myapp",
        username="postgres",
        host="localhost",
        port=5432,
        ssl_mode="prefer",
    )

    fallback_pw = "fallback_secret_789"
    with patch(
        "support.application.connection.ConnectionTestWorkflow.resolve_saved_password",
        side_effect=CredentialRetrievalError("Secure store device I/O error"),
    ), patch("support.cli.profile.connection._is_interactive", return_value=True), patch(
        "support.infrastructure.dispatcher.ConnectorDispatcher.test_connection", return_value=mock_result
    ) as mock_test:
        res = runner.invoke(app, ["profile", "test", "keyring-fail-interactive"], input=f"{fallback_pw}\n", env=env)
        assert res.exit_code == 0
        assert "Warning: Could not retrieve saved password from secure store: Secure store device I/O error" in res.output
        assert "Falling back to one-time password entry for this connection test." in res.output
        assert "Connection succeeded." in res.output
        mock_test.assert_called_once()
        assert mock_test.call_args.kwargs["password"] == fallback_pw






