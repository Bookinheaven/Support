"""Unit and integration tests for Rich CLI presentation and status formatting."""

import os
from unittest.mock import MagicMock, patch
import pytest

from support.application.ports import ConnectionTestResult
from support.cli.presentation import (
    DatabaseStatusInfo,
    format_duration,
    format_endpoint,
    format_engine_name,
    get_console,
    print_status,
    render_database_status_line,
    render_model_status_line,
)
from support.domain.profiles import (
    DatabaseType,
    MariaDBConnection,
    MongoDBConnection,
    MySQLConnection,
    PostgresConnection,
    SQLiteConnection,
    SQLServerConnection,
)


def test_format_engine_names_all_types() -> None:
    """Verify friendly name mapping for all supported database engines."""
    assert format_engine_name("postgresql") == "PostgreSQL"
    assert format_engine_name("mysql") == "MySQL"
    assert format_engine_name("mariadb") == "MariaDB"
    assert format_engine_name("sqlite") == "SQLite"
    assert format_engine_name("sqlserver") == "SQL Server"
    assert format_engine_name("mongodb") == "MongoDB"
    assert format_engine_name(DatabaseType.POSTGRESQL) == "PostgreSQL"
    assert format_engine_name(DatabaseType.SQLSERVER) == "SQL Server"
    assert format_engine_name(DatabaseType.MONGODB) == "MongoDB"
    assert format_engine_name(None) == "Unknown"
    assert format_engine_name("custom_engine") == "Custom_engine"


def test_format_duration_scale() -> None:
    """Verify duration formatting across millisecond, sub-millisecond, and multi-second scales."""
    assert format_duration(None) == ""
    assert format_duration(0.0001) == "< 1 ms"
    assert format_duration(0.0009) == "< 1 ms"
    assert format_duration(0.042) == "42 ms"
    assert format_duration(0.113) == "113 ms"
    assert format_duration(0.999) == "999 ms"
    assert format_duration(1.000) == "1.00 s"
    assert format_duration(2.456) == "2.46 s"


def test_format_endpoints_across_databases() -> None:
    """Verify endpoint formatting for all connection types and secret sanitization."""
    # Postgres
    pg = PostgresConnection(host="pg.internal", port=5432, database="prod", username="admin", ssl_mode="prefer")
    assert format_endpoint(pg) == "pg.internal:5432"

    # MySQL
    my = MySQLConnection(host="mysql.internal", port=3306, database="catalog", username="root", ssl_mode="preferred")
    assert format_endpoint(my) == "mysql.internal:3306"

    # MariaDB
    maria = MariaDBConnection(host="maria.internal", port=3307, database="crm", username="analyst", ssl_mode="prefer")
    assert format_endpoint(maria) == "maria.internal:3307"

    # SQL Server
    mssql = SQLServerConnection(host="sql.internal", port=1433, database="master", username="sa")
    assert format_endpoint(mssql) == "sql.internal:1433"

    # SQLite
    sqlite_file = SQLiteConnection(database_path="/var/data/support.db")
    assert format_endpoint(sqlite_file) == "/var/data/support.db"
    sqlite_mem = SQLiteConnection(database_path=":memory:")
    assert format_endpoint(sqlite_mem) == ":memory:"

    # MongoDB
    mongo = MongoDBConnection(uri="mongodb://mongo.internal:27017", database="analytics")
    assert format_endpoint(mongo) == "mongo.internal:27017"

    # MongoDB SRV cluster
    mongo_srv = MongoDBConnection(uri="mongodb+srv://cluster.internal/test?ssl=true", database="test")
    assert format_endpoint(mongo_srv) == "cluster.internal"

    assert format_endpoint(None) == "none"


def test_render_database_status_online_with_duration() -> None:
    """Online database status displays profile name, engine, and measured duration."""
    info = DatabaseStatusInfo(
        profile_name="analytics-pg",
        engine_name="PostgreSQL",
        is_online=True,
        duration_seconds=0.042,
    )
    rendered = render_database_status_line(info)
    assert "Database:" in rendered
    assert "analytics-pg (PostgreSQL)" in rendered
    assert "Online" in rendered
    assert "42 ms" in rendered


def test_render_database_status_offline_with_reason() -> None:
    """Offline database status displays failure status and concise reason."""
    info = DatabaseStatusInfo(
        profile_name="staging-db",
        engine_name="MySQL",
        is_online=False,
        failure_reason="Authentication failed for user 'app'",
    )
    rendered = render_database_status_line(info)
    assert "staging-db (MySQL)" in rendered
    assert "Offline" in rendered
    assert "Reason: Authentication failed for user 'app'" in rendered


def test_render_database_status_not_configured() -> None:
    """When no profile is active, displays (none) and Not configured."""
    info = DatabaseStatusInfo(profile_name="(none)")
    rendered = render_database_status_line(info)
    assert "(none)" in rendered
    assert "Not configured" in rendered


def test_render_model_status_variants() -> None:
    """Model status renders Not configured, Available, and Unavailable."""
    # Not configured (default state)
    assert "Not configured" in render_model_status_line(None)

    # Available
    avail = render_model_status_line("ollama/qwen2.5-coder:7b", is_available=True)
    assert "ollama/qwen2.5-coder:7b" in avail
    assert "Available" in avail

    # Unavailable
    unavail = render_model_status_line("ollama/qwen2.5-coder:7b", is_available=False)
    assert "ollama/qwen2.5-coder:7b" in unavail
    assert "Unavailable" in unavail


def test_print_status_output_details(capsys: pytest.CaptureFixture[str]) -> None:
    """print_status displays compact status lines and aligned metadata details when available."""
    console = get_console()
    info = DatabaseStatusInfo(
        profile_name="prod-pg",
        engine_name="PostgreSQL",
        database_name="app_prod",
        endpoint="db.internal:5432",
        server_version="PostgreSQL 17.2",
        is_online=True,
        duration_seconds=0.113,
    )
    print_status(console, info)

    captured = capsys.readouterr().out
    assert "Database:  prod-pg (PostgreSQL)  ·  Online  ·  113 ms" in captured
    assert "Model:     Not configured" in captured
    assert "Database:  app_prod" in captured
    assert "Endpoint:  db.internal:5432" in captured
    assert "Server:    PostgreSQL 17.2" in captured


def test_print_status_omits_missing_metadata(capsys: pytest.CaptureFixture[str]) -> None:
    """print_status omits unavailable fields without printing None or inventing values."""
    console = get_console()
    info = DatabaseStatusInfo(
        profile_name="local-db",
        engine_name="SQLite",
        is_online=True,
        database_name=None,
        endpoint=None,
        server_version=None,
    )
    print_status(console, info)

    captured = capsys.readouterr().out
    assert "Database:  local-db (SQLite)  ·  Online" in captured
    assert "None" not in captured
    assert "Server:" not in captured
    assert "Endpoint:" not in captured


def test_no_color_environment_respected(monkeypatch: pytest.MonkeyPatch) -> None:
    """Respect NO_COLOR environment variable and disable terminal styling."""
    monkeypatch.setenv("NO_COLOR", "1")
    console = get_console()
    assert console.no_color is True
