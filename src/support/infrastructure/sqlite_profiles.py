"""SQLite implementation of the ProfileRepository port with typed multi-database support."""

from __future__ import annotations

from collections.abc import Generator
from contextlib import contextmanager
import os
from pathlib import Path
import sqlite3
from uuid import UUID

from support.application.errors import (
    ProfileAlreadyExistsError,
    ProfileNotFoundError,
    ProfileStorageError,
    UnsupportedSchemaVersionError,
)
from support.domain.profiles import (
    ConnectionConfig,
    DatabaseProfile,
)
from support.infrastructure.config import get_database_path
from support.infrastructure.profile_config_codec import (
    deserialize_connection_config,
    serialize_connection_config,
)

CURRENT_SCHEMA_VERSION = 1
BUSY_TIMEOUT_MS = 5000


class SQLiteProfileRepository:
    """SQLite-backed repository for storing and managing database profiles."""

    def __init__(self, db_path: Path | None = None) -> None:
        self._db_path = db_path if db_path is not None else get_database_path()

    @property
    def database_path(self) -> Path:
        """Return the active SQLite database path."""
        return self._db_path

    def _ensure_parent_directory(self) -> None:
        parent = self._db_path.parent
        parent.mkdir(parents=True, exist_ok=True)
        if os.name == "posix":
            try:
                os.chmod(parent, 0o700)
            except OSError:
                pass

    def _ensure_file_permissions(self) -> None:
        if os.name == "posix" and self._db_path.exists():
            try:
                os.chmod(self._db_path, 0o600)
            except OSError:
                pass

    def _ensure_schema(self, conn: sqlite3.Connection) -> None:
        cursor = conn.execute("PRAGMA user_version;")
        row = cursor.fetchone()
        version = row[0] if row else 0

        if version > CURRENT_SCHEMA_VERSION:
            raise UnsupportedSchemaVersionError(
                f"Unsupported database schema version {version}. "
                f"Maximum supported schema version is {CURRENT_SCHEMA_VERSION}. "
                "Please back up and recreate your database manually."
            )

        if version == 0:
            # Prevent claiming an existing database with unrecognized tables
            cursor = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%';"
            )
            existing_tables = [r[0] for r in cursor.fetchall()]
            if existing_tables:
                raise ProfileStorageError(
                    f"Database at '{self._db_path}' contains existing unrecognized tables "
                    f"({', '.join(existing_tables)}). Refusing to claim or overwrite an unknown database."
                )

            with conn:
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS profiles (
                        id TEXT PRIMARY KEY NOT NULL,
                        name TEXT UNIQUE NOT NULL,
                        database_type TEXT NOT NULL,
                        connection_config TEXT NOT NULL
                    );
                    """
                )
                conn.execute(f"PRAGMA user_version = {CURRENT_SCHEMA_VERSION};")
        elif version == 1:
            # Verify table 'profiles' exists in version 1
            cursor = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='profiles';"
            )
            if not cursor.fetchone():
                raise ProfileStorageError(
                    f"Database at '{self._db_path}' has version 1 but is missing the 'profiles' table. "
                    "Please back up and recreate your database manually."
                )

            info = conn.execute("PRAGMA table_info(profiles);").fetchall()
            col_map = {r[1]: r for r in info}
            actual_columns = set(col_map.keys())
            canonical_columns = {"id", "name", "database_type", "connection_config"}

            # Detect old PostgreSQL-only development layout or any non-canonical column layout
            old_postgres_columns = {"database_name", "username", "host", "port", "ssl_mode"}
            if old_postgres_columns.intersection(actual_columns) or actual_columns != canonical_columns:
                raise UnsupportedSchemaVersionError(
                    f"Database at '{self._db_path}' uses an obsolete development schema layout. "
                    "Automatic migration is not supported in this pre-release. "
                    "Please back up and recreate your database manually."
                )

            # Validate canonical schema: exact column types and NOT NULL constraints
            canonical_types = {
                "id": "TEXT",
                "name": "TEXT",
                "database_type": "TEXT",
                "connection_config": "TEXT",
            }
            for col_name, expected_type in canonical_types.items():
                col = col_map.get(col_name)
                if col is None:
                    raise ProfileStorageError(
                        f"Database at '{self._db_path}' is missing expected column '{col_name}'. "
                        "Please back up and recreate your database manually."
                    )
                if col[2].upper() != expected_type:
                    raise ProfileStorageError(
                        f"Database at '{self._db_path}' has invalid type for column '{col_name}': "
                        f"expected {expected_type}, got {col[2]}. "
                        "Please back up and recreate your database manually."
                    )
                if col[3] != 1:
                    raise ProfileStorageError(
                        f"Database at '{self._db_path}' has an incompatible schema: "
                        f"'{col_name}' column must be NOT NULL. "
                        "Please back up and recreate your database manually."
                    )

            # Validate id is PRIMARY KEY
            if col_map["id"][5] != 1:
                raise ProfileStorageError(
                    f"Database at '{self._db_path}' has an incompatible schema: "
                    "'id' column must be PRIMARY KEY. "
                    "Please back up and recreate your database manually."
                )

            # Validate name is UNIQUE (via unique index or constraint)
            unique_columns = set()
            for idx in conn.execute("PRAGMA index_list(profiles);").fetchall():
                if idx[2] == 1:
                    for col_idx in conn.execute(f"PRAGMA index_info({idx[1]});").fetchall():
                        unique_columns.add(col_idx[2])
            if "name" not in unique_columns:
                raise ProfileStorageError(
                    f"Database at '{self._db_path}' has an incompatible schema: "
                    "'name' column must be UNIQUE. "
                    "Please back up and recreate your database manually."
                )

    @contextmanager
    def _connect(self) -> Generator[sqlite3.Connection]:
        self._ensure_parent_directory()
        conn = sqlite3.connect(str(self._db_path), timeout=BUSY_TIMEOUT_MS / 1000.0)
        try:
            conn.row_factory = sqlite3.Row
            conn.execute(f"PRAGMA busy_timeout = {BUSY_TIMEOUT_MS};")
            self._ensure_schema(conn)
            self._ensure_file_permissions()
            yield conn
        finally:
            conn.close()

    def _row_to_profile(self, row: sqlite3.Row) -> DatabaseProfile:
        try:
            profile_id = UUID(row["id"])
            connection = deserialize_connection_config(
                database_type=row["database_type"],
                config_json=row["connection_config"],
            )
            return DatabaseProfile(
                name=row["name"],
                connection=connection,
                id=profile_id,
            )
        except ProfileStorageError:
            raise
        except Exception as exc:
            raise ProfileStorageError(
                f"Corrupt or invalid profile record in database: {exc}"
            ) from exc

    def add(self, profile: DatabaseProfile) -> None:
        db_type, config_json = serialize_connection_config(profile.connection)
        query = """
        INSERT INTO profiles (id, name, database_type, connection_config)
        VALUES (?, ?, ?, ?);
        """
        params = (
            str(profile.id),
            profile.name,
            db_type,
            config_json,
        )
        try:
            with self._connect() as conn:
                with conn:
                    conn.execute(query, params)
        except sqlite3.IntegrityError as exc:
            err_msg = str(exc).lower()
            if "profiles.name" in err_msg or ("unique" in err_msg and "name" in err_msg):
                raise ProfileAlreadyExistsError(
                    f"A profile with name '{profile.name}' already exists."
                ) from exc
            raise ProfileStorageError(
                f"Integrity violation adding profile '{profile.name}': {exc}"
            ) from exc
        except sqlite3.Error as exc:
            raise ProfileStorageError(
                f"Failed to add profile '{profile.name}': {exc}"
            ) from exc

    def get_by_name(self, name: str) -> DatabaseProfile | None:
        if not self._db_path.exists():
            return None

        query = """
        SELECT id, name, database_type, connection_config
        FROM profiles
        WHERE name = ?;
        """
        try:
            with self._connect() as conn:
                cursor = conn.execute(query, (name,))
                row = cursor.fetchone()
                if row is None:
                    return None
                return self._row_to_profile(row)
        except sqlite3.Error as exc:
            raise ProfileStorageError(
                f"Failed to retrieve profile '{name}': {exc}"
            ) from exc

    def list_all(self) -> list[DatabaseProfile]:
        if not self._db_path.exists():
            return []

        query = """
        SELECT id, name, database_type, connection_config
        FROM profiles
        ORDER BY name ASC;
        """
        try:
            with self._connect() as conn:
                cursor = conn.execute(query)
                rows = cursor.fetchall()
                return [self._row_to_profile(row) for row in rows]
        except sqlite3.Error as exc:
            raise ProfileStorageError(
                f"Failed to list profiles: {exc}"
            ) from exc

    def update(self, profile: DatabaseProfile) -> None:
        db_type, config_json = serialize_connection_config(profile.connection)
        query = """
        UPDATE profiles
        SET name = ?, database_type = ?, connection_config = ?
        WHERE id = ?;
        """
        params = (
            profile.name,
            db_type,
            config_json,
            str(profile.id),
        )
        try:
            with self._connect() as conn:
                with conn:
                    cursor = conn.execute(query, params)
                    if cursor.rowcount == 0:
                        raise ProfileNotFoundError(f"Profile '{profile.name}' not found.")
        except sqlite3.IntegrityError as exc:
            err_msg = str(exc).lower()
            if "profiles.name" in err_msg or ("unique" in err_msg and "name" in err_msg):
                raise ProfileAlreadyExistsError(
                    f"A profile with name '{profile.name}' already exists."
                ) from exc
            raise ProfileStorageError(
                f"Integrity violation updating profile '{profile.name}': {exc}"
            ) from exc
        except sqlite3.Error as exc:
            raise ProfileStorageError(
                f"Failed to update profile '{profile.name}': {exc}"
            ) from exc

    def delete(self, name: str) -> None:
        if not self._db_path.exists():
            raise ProfileNotFoundError(f"Profile '{name}' not found.")

        query = "DELETE FROM profiles WHERE name = ?;"
        try:
            with self._connect() as conn:
                with conn:
                    cursor = conn.execute(query, (name,))
                    if cursor.rowcount == 0:
                        raise ProfileNotFoundError(f"Profile '{name}' not found.")
        except sqlite3.Error as exc:
            raise ProfileStorageError(
                f"Failed to delete profile '{name}': {exc}"
            ) from exc
