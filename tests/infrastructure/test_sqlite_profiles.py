"""Tests for SQLiteProfileRepository."""

from pathlib import Path
import sqlite3
from uuid import UUID, uuid4

import pytest

from support.application.errors import (
    ProfileAlreadyExistsError,
    ProfileNotFoundError,
    ProfileStorageError,
    UnsupportedSchemaVersionError,
)
from support.domain.profiles import DatabaseProfile, PostgresConnection
from support.infrastructure.sqlite_profiles import SQLiteProfileRepository


def _make_profile(
    name: str = "test-db",
    database: str = "app_db",
    username: str = "app_user",
    ssl_mode: str = "prefer",
    host: str = "localhost",
    port: int = 5432,
    profile_id: UUID | None = None,
) -> DatabaseProfile:
    conn = PostgresConnection(
        database=database,
        username=username,
        ssl_mode=ssl_mode,
        host=host,
        port=port,
    )
    if profile_id is not None:
        return DatabaseProfile(name=name, connection=conn, id=profile_id)
    return DatabaseProfile(name=name, connection=conn)


def test_profile_persistence_and_retrieval(tmp_path: Path) -> None:
    db_path = tmp_path / "profiles.sqlite3"
    repo = SQLiteProfileRepository(db_path)

    profile = _make_profile(name="primary-db", database="prod", username="admin", ssl_mode="require")
    repo.add(profile)

    retrieved = repo.get_by_name("primary-db")
    assert retrieved is not None
    assert retrieved.id == profile.id
    assert retrieved.name == "primary-db"
    assert retrieved.connection.database == "prod"
    assert retrieved.connection.username == "admin"
    assert retrieved.connection.ssl_mode == "require"
    assert retrieved.connection.host == "localhost"
    assert retrieved.connection.port == 5432

    # Non-existent profile returns None
    assert repo.get_by_name("non-existent") is None


def test_profile_listing_deterministic_ordering(tmp_path: Path) -> None:
    db_path = tmp_path / "profiles.sqlite3"
    repo = SQLiteProfileRepository(db_path)

    # Insert out of order
    repo.add(_make_profile(name="zebra-db"))
    repo.add(_make_profile(name="alpha-db"))
    repo.add(_make_profile(name="middle-db"))

    profiles = repo.list_all()
    names = [p.name for p in profiles]
    assert names == ["alpha-db", "middle-db", "zebra-db"]


def test_duplicate_profile_name_rejected(tmp_path: Path) -> None:
    db_path = tmp_path / "profiles.sqlite3"
    repo = SQLiteProfileRepository(db_path)

    profile1 = _make_profile(name="duplicate-db")
    repo.add(profile1)

    profile2 = _make_profile(name="duplicate-db")
    with pytest.raises(ProfileAlreadyExistsError, match="already exists"):
        repo.add(profile2)


def test_profile_rename_preserves_uuid(tmp_path: Path) -> None:
    db_path = tmp_path / "profiles.sqlite3"
    repo = SQLiteProfileRepository(db_path)

    original = _make_profile(name="old-name")
    repo.add(original)

    renamed = original.rename("new-name")
    repo.update(renamed)

    assert repo.get_by_name("old-name") is None
    retrieved = repo.get_by_name("new-name")
    assert retrieved is not None
    assert retrieved.id == original.id
    assert retrieved.name == "new-name"
    assert retrieved.connection == original.connection


def test_profile_rename_to_existing_name_fails(tmp_path: Path) -> None:
    db_path = tmp_path / "profiles.sqlite3"
    repo = SQLiteProfileRepository(db_path)

    repo.add(_make_profile(name="profile-one"))
    p2 = _make_profile(name="profile-two")
    repo.add(p2)

    renamed_p2 = p2.rename("profile-one")
    with pytest.raises(ProfileAlreadyExistsError, match="already exists"):
        repo.update(renamed_p2)


def test_profile_deletion(tmp_path: Path) -> None:
    db_path = tmp_path / "profiles.sqlite3"
    repo = SQLiteProfileRepository(db_path)

    profile = _make_profile(name="to-delete")
    repo.add(profile)
    assert repo.get_by_name("to-delete") is not None

    repo.delete("to-delete")
    assert repo.get_by_name("to-delete") is None

    # Deleting missing profile raises ProfileNotFoundError
    with pytest.raises(ProfileNotFoundError, match="not found"):
        repo.delete("to-delete")


def test_persistence_across_repository_instances(tmp_path: Path) -> None:
    db_path = tmp_path / "profiles.sqlite3"
    repo1 = SQLiteProfileRepository(db_path)

    profile = _make_profile(name="cross-instance")
    repo1.add(profile)

    # Separate repository instance pointing to the exact same file
    repo2 = SQLiteProfileRepository(db_path)
    retrieved = repo2.get_by_name("cross-instance")
    assert retrieved is not None
    assert retrieved.id == profile.id
    assert retrieved.name == "cross-instance"


def test_unsupported_schema_version_rejected(tmp_path: Path) -> None:
    db_path = tmp_path / "profiles.sqlite3"
    # Pre-create database with a future schema version
    conn = sqlite3.connect(str(db_path))
    with conn:
        conn.execute("PRAGMA user_version = 99;")
    conn.close()

    repo = SQLiteProfileRepository(db_path)
    with pytest.raises(UnsupportedSchemaVersionError, match="Unsupported database schema version 99"):
        repo.list_all()


def test_no_secrets_in_sqlite_schema(tmp_path: Path) -> None:
    db_path = tmp_path / "profiles.sqlite3"
    repo = SQLiteProfileRepository(db_path)
    repo.add(_make_profile(name="audit-profile"))

    conn = sqlite3.connect(str(db_path))
    cursor = conn.execute("PRAGMA table_info(profiles);")
    columns = [row[1] for row in cursor.fetchall()]
    conn.close()

    expected_columns = {"id", "name", "database_type", "connection_config"}
    assert set(columns) == expected_columns
    assert "password" not in columns
    assert "secret" not in columns
    assert "keyring" not in columns
    assert "url" not in columns


def test_schema_enforces_id_not_null_and_version_1(tmp_path: Path) -> None:
    db_path = tmp_path / "profiles.sqlite3"
    repo = SQLiteProfileRepository(db_path)
    repo.add(_make_profile(name="nn-check"))

    conn = sqlite3.connect(str(db_path))
    cursor = conn.execute("PRAGMA table_info(profiles);")
    col_map = {row[1]: row for row in cursor.fetchall()}
    v_row = conn.execute("PRAGMA user_version;").fetchone()
    conn.close()

    assert v_row[0] == 1, "Canonical schema must set PRAGMA user_version = 1"
    # row structure: (cid, name, type, notnull, dflt_value, pk)
    assert col_map["id"][3] == 1, "id column must have NOT NULL constraint"


def test_canonical_version_1_opens_normally(tmp_path: Path) -> None:
    db_path = tmp_path / "profiles.sqlite3"
    profile_id = str(uuid4())
    conn = sqlite3.connect(str(db_path))
    with conn:
        conn.execute(
            """
            CREATE TABLE profiles (
                id TEXT PRIMARY KEY NOT NULL,
                name TEXT UNIQUE NOT NULL,
                database_type TEXT NOT NULL,
                connection_config TEXT NOT NULL
            );
            """
        )
        conn.execute("PRAGMA user_version = 1;")
        conn.execute(
            "INSERT INTO profiles VALUES (?, ?, ?, ?);",
            (profile_id, "canon-prof", "sqlite", '{"database_path": "/data/test.db"}'),
        )
    conn.close()

    repo = SQLiteProfileRepository(db_path)
    p = repo.get_by_name("canon-prof")
    assert p is not None
    assert p.name == "canon-prof"
    assert p.connection.database_path == "/data/test.db"


def test_obsolete_version_1_schema_rejected_without_modification(tmp_path: Path) -> None:
    db_path = tmp_path / "profiles.sqlite3"
    conn = sqlite3.connect(str(db_path))
    with conn:
        conn.execute(
            """
            CREATE TABLE profiles (
                id TEXT PRIMARY KEY NOT NULL,
                name TEXT UNIQUE NOT NULL,
                database_name TEXT NOT NULL,
                username TEXT NOT NULL,
                host TEXT NOT NULL,
                port INTEGER NOT NULL CHECK(port >= 1 AND port <= 65535),
                ssl_mode TEXT NOT NULL
            );
            """
        )
        conn.execute("PRAGMA user_version = 1;")
        conn.execute(
            """
            INSERT INTO profiles VALUES (
                '11111111-1111-1111-1111-111111111111',
                'legacy-dev',
                'legacydb',
                'user',
                'localhost',
                5432,
                'prefer'
            );
            """
        )
    conn.close()

    repo = SQLiteProfileRepository(db_path)
    with pytest.raises(UnsupportedSchemaVersionError, match="obsolete development schema layout"):
        repo.get_by_name("legacy-dev")

    # Verify database was NOT modified or silently repaired
    conn = sqlite3.connect(str(db_path))
    rows = conn.execute("SELECT * FROM profiles;").fetchall()
    v_row = conn.execute("PRAGMA user_version;").fetchone()
    conn.close()

    assert v_row[0] == 1, "Schema version must remain 1"
    assert len(rows) == 1, "Existing profile rows must remain untouched"


def test_version_2_schema_rejected_without_modification(tmp_path: Path) -> None:
    db_path = tmp_path / "profiles.sqlite3"
    conn = sqlite3.connect(str(db_path))
    with conn:
        conn.execute(
            """
            CREATE TABLE profiles (
                id TEXT PRIMARY KEY NOT NULL,
                name TEXT UNIQUE NOT NULL,
                database_type TEXT NOT NULL,
                connection_config TEXT NOT NULL
            );
            """
        )
        conn.execute("PRAGMA user_version = 2;")
        conn.execute(
            "INSERT INTO profiles VALUES ('22222222-2222-2222-2222-222222222222', 'v2-prof', 'sqlite', '{\"database_path\": \"/tmp/db.sqlite\"}');"
        )
    conn.close()

    repo = SQLiteProfileRepository(db_path)
    with pytest.raises(UnsupportedSchemaVersionError, match="Unsupported database schema version 2"):
        repo.get_by_name("v2-prof")

    # Verify database was NOT modified
    conn = sqlite3.connect(str(db_path))
    rows = conn.execute("SELECT * FROM profiles;").fetchall()
    v_row = conn.execute("PRAGMA user_version;").fetchone()
    conn.close()

    assert v_row[0] == 2, "Schema version must remain 2"
    assert len(rows) == 1, "Existing profile rows must remain untouched"


def test_version_1_nullable_id_schema_rejected(tmp_path: Path) -> None:
    db_path = tmp_path / "profiles.sqlite3"
    conn = sqlite3.connect(str(db_path))
    with conn:
        conn.execute(
            """
            CREATE TABLE profiles (
                id TEXT PRIMARY KEY,
                name TEXT UNIQUE NOT NULL,
                database_type TEXT NOT NULL,
                connection_config TEXT NOT NULL
            );
            """
        )
        conn.execute("PRAGMA user_version = 1;")
    conn.close()

    repo = SQLiteProfileRepository(db_path)
    with pytest.raises(ProfileStorageError, match="incompatible schema: 'id' column must be NOT NULL"):
        repo.get_by_name("test")


def test_unrecognized_database_rejected(tmp_path: Path) -> None:
    db_path = tmp_path / "profiles.sqlite3"
    conn = sqlite3.connect(str(db_path))
    with conn:
        conn.execute("CREATE TABLE foreign_table (data TEXT);")
    conn.close()

    repo = SQLiteProfileRepository(db_path)
    with pytest.raises(ProfileStorageError, match="existing unrecognized tables"):
        repo.add(_make_profile(name="new-profile"))


def test_id_collision_raises_storage_error_not_duplicate_name(tmp_path: Path) -> None:
    db_path = tmp_path / "profiles.sqlite3"
    repo = SQLiteProfileRepository(db_path)

    fixed_id = uuid4()
    p1 = _make_profile(name="name-one", profile_id=fixed_id)
    repo.add(p1)

    p2 = _make_profile(name="name-two", profile_id=fixed_id)
    # Should raise ProfileStorageError for PK conflict, NOT ProfileAlreadyExistsError
    with pytest.raises(ProfileStorageError, match="Integrity violation"):
        repo.add(p2)


def test_read_operations_do_not_create_database_file(tmp_path: Path) -> None:
    db_path = tmp_path / "subfolder" / "profiles.sqlite3"
    repo = SQLiteProfileRepository(db_path)

    assert repo.list_all() == []
    assert repo.get_by_name("non-existent") is None
    assert not db_path.exists()
    assert not db_path.parent.exists()


def test_multi_database_serialization_round_trip(tmp_path: Path) -> None:
    from support.domain.profiles import (
        MariaDBConnection,
        MongoDBConnection,
        MySQLConnection,
        PostgresConnection,
        SQLiteConnection,
        SQLServerConnection,
    )

    db_path = tmp_path / "profiles.sqlite3"
    repo = SQLiteProfileRepository(db_path)

    configs = [
        PostgresConnection(database="pgdb", username="pguser", ssl_mode="require"),
        MySQLConnection(database="mysqldb", username="myuser", host="127.0.0.1", port=3307),
        MariaDBConnection(database="mariadb", username="mariauser", ssl_mode="prefer"),
        SQLiteConnection(database_path="/var/data/app.db"),
        SQLServerConnection(database="sqlsrv", username="sa", encrypt=False),
        MongoDBConnection(database="mongodb", username="mongouser", tls=True),
    ]

    for i, cfg in enumerate(configs):
        profile = DatabaseProfile(name=f"db-profile-{i}", connection=cfg)
        repo.add(profile)
        retrieved = repo.get_by_name(f"db-profile-{i}")
        assert retrieved is not None
        assert retrieved.id == profile.id
        assert retrieved.name == profile.name
        assert retrieved.connection == cfg
        assert retrieved.database_type == cfg.database_type


def test_unknown_database_type_rejected(tmp_path: Path) -> None:
    db_path = tmp_path / "profiles.sqlite3"
    repo = SQLiteProfileRepository(db_path)
    # Add a valid profile to initialize schema v2
    repo.add(_make_profile(name="seed"))

    conn = sqlite3.connect(str(db_path))
    with conn:
        conn.execute(
            "INSERT INTO profiles (id, name, database_type, connection_config) VALUES (?, ?, ?, ?);",
            (str(uuid4()), "unknown-db", "couchdb", '{"database": "test"}'),
        )
    conn.close()

    with pytest.raises(ProfileStorageError, match="Unknown database type 'couchdb'"):
        repo.get_by_name("unknown-db")


def test_malformed_json_config_rejected(tmp_path: Path) -> None:
    db_path = tmp_path / "profiles.sqlite3"
    repo = SQLiteProfileRepository(db_path)
    repo.add(_make_profile(name="seed"))

    conn = sqlite3.connect(str(db_path))
    with conn:
        conn.execute(
            "INSERT INTO profiles (id, name, database_type, connection_config) VALUES (?, ?, ?, ?);",
            (str(uuid4()), "bad-json", "sqlite", "this is not json"),
        )
    conn.close()

    with pytest.raises(ProfileStorageError, match="Malformed JSON"):
        repo.get_by_name("bad-json")
