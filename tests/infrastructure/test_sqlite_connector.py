"""Unit tests for SQLiteConnector infrastructure adapter."""

import os
from pathlib import Path
import sqlite3
import pytest

from support.application.errors import (
    DatabaseNotFoundError,
    GeneralConnectionError,
)
from support.domain.profiles import SQLiteConnection
from support.infrastructure.sqlite_connector import SQLiteConnector


def _create_sample_database(db_path: Path) -> None:
    conn = sqlite3.connect(str(db_path))
    with conn:
        conn.execute("CREATE TABLE users (id INTEGER PRIMARY KEY, name TEXT);")
        conn.execute("INSERT INTO users VALUES (1, 'Alice'), (2, 'Bob');")
    conn.close()


def test_sqlite_connector_existing_file_success(tmp_path: Path) -> None:
    db_file = tmp_path / "app.db"
    _create_sample_database(db_file)

    connector = SQLiteConnector()
    conn_config = SQLiteConnection(database_path=str(db_file))

    result = connector.test_connection(conn_config)

    assert result.success is True
    assert result.database_type == "sqlite"
    assert result.database == "app.db"
    assert result.host is None
    assert result.port is None
    assert result.username is None
    assert result.ssl_mode is None
    assert result.server_version == sqlite3.sqlite_version
    assert result.message == "Connection successful."


def test_sqlite_connector_memory_success() -> None:
    connector = SQLiteConnector()
    conn_config = SQLiteConnection(database_path=":memory:")

    result = connector.test_connection(conn_config)

    assert result.success is True
    assert result.database_type == "sqlite"
    assert result.database == ":memory:"
    assert result.server_version == sqlite3.sqlite_version


def test_sqlite_connector_missing_file_does_not_create_database(tmp_path: Path) -> None:
    missing_file = tmp_path / "nonexistent.db"
    assert not missing_file.exists()

    connector = SQLiteConnector()
    conn_config = SQLiteConnection(database_path=str(missing_file))

    with pytest.raises(DatabaseNotFoundError, match="SQLite database file not found"):
        connector.test_connection(conn_config)

    # CRITICAL: Missing file must NOT be created by connection testing
    assert not missing_file.exists()


def test_sqlite_connector_leaves_database_file_unchanged(tmp_path: Path) -> None:
    db_file = tmp_path / "prod.db"
    _create_sample_database(db_file)
    initial_bytes = db_file.read_bytes()

    connector = SQLiteConnector()
    conn_config = SQLiteConnection(database_path=str(db_file))
    result = connector.test_connection(conn_config)
    assert result.success is True

    # Database content must be byte-for-byte identical (no journals, no writes)
    assert db_file.read_bytes() == initial_bytes


def test_sqlite_connector_directory_path_rejected(tmp_path: Path) -> None:
    connector = SQLiteConnector()
    conn_config = SQLiteConnection(database_path=str(tmp_path))

    with pytest.raises(GeneralConnectionError, match="Provided path is a directory"):
        connector.test_connection(conn_config)


def test_sqlite_connector_malformed_database_rejected(tmp_path: Path) -> None:
    bad_file = tmp_path / "corrupt.db"
    bad_file.write_text("THIS IS NOT A VALID SQLITE DATABASE FILE")

    connector = SQLiteConnector()
    conn_config = SQLiteConnection(database_path=str(bad_file))

    with pytest.raises(GeneralConnectionError, match="Invalid or malformed SQLite database"):
        connector.test_connection(conn_config)


def test_sqlite_connector_zero_byte_file_rejected(tmp_path: Path) -> None:
    zero_file = tmp_path / "zero.db"
    zero_file.touch()
    assert zero_file.stat().st_size == 0

    connector = SQLiteConnector()
    conn_config = SQLiteConnection(database_path=str(zero_file))

    with pytest.raises(GeneralConnectionError, match="empty file \\(0 bytes\\)"):
        connector.test_connection(conn_config)

    # File must remain 0 bytes
    assert zero_file.stat().st_size == 0


def test_sqlite_connector_truncated_header_rejected(tmp_path: Path) -> None:
    short_file = tmp_path / "short.db"
    short_file.write_bytes(b"SQLite format 3\x00" + b"\x00" * 30)  # 46 bytes < 100 bytes

    connector = SQLiteConnector()
    conn_config = SQLiteConnection(database_path=str(short_file))

    with pytest.raises(GeneralConnectionError, match="smaller than the 100-byte SQLite header"):
        connector.test_connection(conn_config)


def test_sqlite_connector_invalid_page_size_rejected(tmp_path: Path) -> None:
    bad_page_file = tmp_path / "bad_page.db"
    # 100 bytes, valid magic, but page size at bytes 16-17 = 3 (not a power of two between 512 and 32768 or 1)
    header = b"SQLite format 3\x00" + (3).to_bytes(2, "big") + b"\x00" * 82
    bad_page_file.write_bytes(header)

    connector = SQLiteConnector()
    conn_config = SQLiteConnection(database_path=str(bad_page_file))

    with pytest.raises(GeneralConnectionError, match="invalid page size"):
        connector.test_connection(conn_config)


def test_sqlite_connector_permission_denied_mapped_to_general_connection_error(tmp_path: Path) -> None:
    db_file = tmp_path / "unreadable.db"
    _create_sample_database(db_file)

    try:
        db_file.chmod(0o000)
        # Verify permissions were actually dropped
        if not os.access(db_file, os.R_OK):
            connector = SQLiteConnector()
            conn_config = SQLiteConnection(database_path=str(db_file))

            # Must raise GeneralConnectionError, NOT HostUnreachableError
            with pytest.raises(GeneralConnectionError, match="Access denied or unreadable"):
                connector.test_connection(conn_config)
    finally:
        db_file.chmod(0o600)

