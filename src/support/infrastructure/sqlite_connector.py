"""SQLite database connectivity implementation."""

from __future__ import annotations

import os
from pathlib import Path
import sqlite3

from support.application.errors import (
    ConnectionTimeoutError,
    DatabaseNotFoundError,
    GeneralConnectionError,
)
from support.application.ports import ConnectionTestResult
from support.domain.profiles import SQLiteConnection

DEFAULT_SQLITE_TIMEOUT_SECONDS = 10.0
SQLITE_HEADER_MAGIC = b"SQLite format 3\x00"
SQLITE_HEADER_SIZE = 100
VALID_PAGE_SIZES = {512, 1024, 2048, 4096, 8192, 16384, 32768, 65536}


def _validate_sqlite_file(path: Path, connection_path: str) -> None:
    """Validate that the file exists, is readable, and contains a valid SQLite header.

    Note: A recognized SQLite header does not guarantee every database page is healthy.
    Full database integrity scanning is intentionally omitted during connection testing.
    """
    if not path.exists():
        raise DatabaseNotFoundError(
            f"SQLite database file not found: '{connection_path}'."
        )

    if path.is_dir():
        raise GeneralConnectionError(
            f"Provided path is a directory, not an SQLite database file: '{connection_path}'."
        )

    if not os.access(path, os.R_OK):
        raise GeneralConnectionError(
            f"Access denied or unreadable SQLite database file: '{connection_path}'."
        )

    try:
        file_size = path.stat().st_size
    except OSError as exc:
        raise GeneralConnectionError(
            f"Unable to read file metadata for SQLite database: '{connection_path}'."
        ) from exc

    if file_size == 0:
        raise GeneralConnectionError(
            f"Invalid or malformed SQLite database: '{connection_path}' is an empty file (0 bytes)."
        )

    if file_size < SQLITE_HEADER_SIZE:
        raise GeneralConnectionError(
            f"Invalid or malformed SQLite database: '{connection_path}' is smaller than the 100-byte SQLite header."
        )

    try:
        with path.open("rb") as f:
            header = f.read(SQLITE_HEADER_SIZE)
    except OSError as exc:
        raise GeneralConnectionError(
            f"Access denied or unreadable SQLite database file: '{connection_path}'."
        ) from exc

    if len(header) < SQLITE_HEADER_SIZE:
        raise GeneralConnectionError(
            f"Invalid or malformed SQLite database: '{connection_path}' has an incomplete header."
        )

    if header[:16] != SQLITE_HEADER_MAGIC:
        raise GeneralConnectionError(
            f"Invalid or malformed SQLite database: '{connection_path}' does not contain a valid SQLite format 3 signature."
        )

    # Validate encoded page size at offset 16 (2 bytes, big-endian)
    page_size_raw = int.from_bytes(header[16:18], byteorder="big")
    page_size = 65536 if page_size_raw == 1 else page_size_raw

    if page_size not in VALID_PAGE_SIZES:
        raise GeneralConnectionError(
            f"Invalid or malformed SQLite database: '{connection_path}' has an invalid page size ({page_size_raw})."
        )

    if file_size < page_size:
        raise GeneralConnectionError(
            f"Invalid or malformed SQLite database: '{connection_path}' is smaller than its declared page size ({page_size} bytes)."
        )


class SQLiteConnector:
    """Infrastructure connector for testing SQLite database connections."""

    def __init__(self, timeout_seconds: float = DEFAULT_SQLITE_TIMEOUT_SECONDS) -> None:
        self._timeout_seconds = timeout_seconds

    def test_connection(
        self,
        connection: SQLiteConnection,
        password: str | None = None,
    ) -> ConnectionTestResult:
        """Test SQLite database connection.

        Opens file-backed databases in read-only mode without executing data-changing queries.
        Supports ':memory:' for transient in-memory databases.
        """
        if not isinstance(connection, SQLiteConnection):
            raise TypeError(
                f"SQLiteConnector requires SQLiteConnection, got {type(connection).__name__}"
            )

        # 1. In-memory database
        if connection.database_path == ":memory:":
            conn = None
            try:
                conn = sqlite3.connect(":memory:", timeout=self._timeout_seconds)
                cur = conn.cursor()
                cur.execute("SELECT 1")
                row = cur.fetchone()
                if row is None or row[0] != 1:
                    raise GeneralConnectionError(
                        f"Database verification query failed: expected 1, got {row}"
                    )
                return ConnectionTestResult(
                    success=True,
                    database_type="sqlite",
                    database=":memory:",
                    server_version=sqlite3.sqlite_version,
                    message="Connection successful.",
                )
            except sqlite3.Error as exc:
                raise GeneralConnectionError(f"SQLite in-memory connection failed: {exc}") from exc
            finally:
                if conn is not None:
                    try:
                        conn.close()
                    except Exception:
                        pass

        # 2. File-backed database
        path = Path(connection.database_path).expanduser()
        if not path.is_absolute():
            path = path.resolve()

        # Validate file existence, permissions, and SQLite header before opening
        _validate_sqlite_file(path, connection.database_path)

        db_identifier = path.name or connection.database_path
        uri = f"{path.as_uri()}?mode=ro"

        conn = None
        try:
            conn = sqlite3.connect(uri, uri=True, timeout=self._timeout_seconds)

            # Read-only schema verification to verify SQLite engine can parse page 1
            conn.execute("PRAGMA schema_version;")

            cur = conn.cursor()
            cur.execute("SELECT 1")
            row = cur.fetchone()
            if row is None or row[0] != 1:
                raise GeneralConnectionError(
                    f"Database verification query failed: expected 1, got {row}"
                )

            return ConnectionTestResult(
                success=True,
                database_type="sqlite",
                database=db_identifier,
                server_version=sqlite3.sqlite_version,
                message="Connection successful.",
            )
        except sqlite3.DatabaseError as exc:
            err_msg = str(exc).lower()
            if "not a database" in err_msg or "malformed" in err_msg or "corrupt" in err_msg:
                raise GeneralConnectionError(
                    f"Invalid or malformed SQLite database: '{connection.database_path}'."
                ) from exc
            raise GeneralConnectionError(f"SQLite connection failed: {exc}") from exc
        except sqlite3.OperationalError as exc:
            err_msg = str(exc).lower()
            if "unable to open database file" in err_msg:
                if not path.exists():
                    raise DatabaseNotFoundError(
                        f"SQLite database file not found: '{connection.database_path}'."
                    ) from exc
                raise GeneralConnectionError(
                    f"Unable to open SQLite database file: '{connection.database_path}'."
                ) from exc
            if "locked" in err_msg or "busy" in err_msg:
                raise ConnectionTimeoutError(
                    f"SQLite database is locked or busy: '{connection.database_path}'."
                ) from exc
            if "permission denied" in err_msg or "access denied" in err_msg:
                raise GeneralConnectionError(
                    f"Access denied to SQLite database file: '{connection.database_path}'."
                ) from exc
            raise GeneralConnectionError(f"SQLite operational error: {exc}") from exc
        except sqlite3.Error as exc:
            raise GeneralConnectionError(f"SQLite error: {exc}") from exc
        finally:
            if conn is not None:
                try:
                    conn.close()
                except Exception:
                    pass
