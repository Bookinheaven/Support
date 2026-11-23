"""CLI tests for support profile test command."""

from pathlib import Path
from unittest.mock import MagicMock, patch
from typer.testing import CliRunner

from support.application.errors import (
    AuthenticationFailedError,
    ConnectionTimeoutError,
    DatabaseNotFoundError,
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

    with patch("support.infrastructure.postgres.PostgresConnector.test_connection", return_value=mock_result) as mock_test:
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
    with patch("support.cli.profile._is_interactive", return_value=True), patch(
        "support.infrastructure.postgres.PostgresConnector.test_connection", return_value=mock_result
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

    with patch("support.cli.profile._is_interactive", return_value=False):
        res = runner.invoke(app, ["profile", "test", "headless-db"], env=env)
        assert res.exit_code == 1
        assert "Error: No saved password for profile 'headless-db' and non-interactive input is not supported." in res.output
        assert "support profile password set headless-db" in res.output


def test_profile_test_auth_failure_with_saved_password_suggests_update(tmp_path: Path) -> None:
    env = {"SUPPORT_HOME": str(tmp_path)}
    _add_test_profile(env, "bad-saved-auth")
    runner.invoke(app, ["profile", "password", "set", "bad-saved-auth"], input="old_pass\nold_pass\n", env=env)

    with patch(
        "support.infrastructure.postgres.PostgresConnector.test_connection",
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
        "support.infrastructure.postgres.PostgresConnector.test_connection",
        side_effect=HostUnreachableError("Connection refused by 'localhost:5432'."),
    ):
        res = runner.invoke(app, ["profile", "test", "err-db"], env=env)
        assert res.exit_code == 1
        assert "Error: Connection refused by 'localhost:5432'." in res.output
        assert "Traceback" not in res.output

    # 2. Connection timeout
    with patch(
        "support.infrastructure.postgres.PostgresConnector.test_connection",
        side_effect=ConnectionTimeoutError("Connection timed out after 10 seconds."),
    ):
        res = runner.invoke(app, ["profile", "test", "err-db"], env=env)
        assert res.exit_code == 1
        assert "Error: Connection timed out after 10 seconds." in res.output
        assert "Traceback" not in res.output

    # 3. Database not found
    with patch(
        "support.infrastructure.postgres.PostgresConnector.test_connection",
        side_effect=DatabaseNotFoundError("Database 'myapp' does not exist."),
    ):
        res = runner.invoke(app, ["profile", "test", "err-db"], env=env)
        assert res.exit_code == 1
        assert "Error: Database 'myapp' does not exist." in res.output

    # 4. SSL verification error
    with patch(
        "support.infrastructure.postgres.PostgresConnector.test_connection",
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
        "support.infrastructure.mysql_connector.MySQLCompatibleConnector.test_connection",
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
        "support.infrastructure.mysql_connector.MySQLCompatibleConnector.test_connection",
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



