"""Tests for live database schema inspection source."""

from pathlib import Path
import sqlite3
import pytest
from sqlalchemy import (
    create_engine,
    MetaData,
    Table,
    Column,
    Integer,
    String,
    ForeignKey,
    ForeignKeyConstraint,
    UniqueConstraint,
)
from support.core.dialect import Dialect
from support.core.exceptions import DatabaseAccessError
from support.db.models import ConnectionProfile
from support.db.connection import DatabaseConnectionFactory
from support.schema.sources.live_db import LiveDatabaseSchemaSource


@pytest.fixture
def test_live_sqlite_db(tmp_path: Path) -> tuple[Path, ConnectionProfile]:
    """Fixture creating an authentic SQLite database and profile for live inspection testing."""
    db_file = tmp_path / "live_test.db"
    conn = sqlite3.connect(db_file)
    cur = conn.cursor()

    cur.execute("""
    CREATE TABLE teams (
        team_id INTEGER PRIMARY KEY AUTOINCREMENT,
        team_name TEXT NOT NULL UNIQUE
    );
    """)

    cur.execute("""
    CREATE TABLE members (
        member_id INTEGER PRIMARY KEY,
        team_id INTEGER NOT NULL,
        name TEXT NOT NULL,
        email TEXT NOT NULL UNIQUE,
        FOREIGN KEY (team_id) REFERENCES teams(team_id) ON DELETE CASCADE
    );
    """)
    conn.commit()
    conn.close()

    profile = ConnectionProfile(
        name="test-sqlite-live",
        dialect=Dialect.SQLITE,
        database=str(db_file),
    )
    return db_file, profile


def test_database_connection_factory_test_connection(test_live_sqlite_db):
    _, profile = test_live_sqlite_db
    success, msg, version_info, latency = DatabaseConnectionFactory.test_connection(profile)

    assert success is True
    assert "Connection successful" in msg
    assert version_info is not None
    assert latency >= 0


def test_live_schema_inspection(test_live_sqlite_db):
    _, profile = test_live_sqlite_db
    source = LiveDatabaseSchemaSource(profile)
    result = source.discover()

    assert not result.has_errors
    assert set(result.schema.tables.keys()) == {"teams", "members"}

    # Validate teams
    teams = result.schema.tables["teams"]
    assert teams.primary_key == ["team_id"]
    team_name = teams.get_column("team_name")
    assert team_name is not None
    assert team_name.is_unique is True

    # Validate members and foreign key
    members = result.schema.tables["members"]
    assert members.primary_key == ["member_id"]
    assert len(members.foreign_keys) == 1
    fk = members.foreign_keys[0]
    assert fk.constrained_columns == ["team_id"]
    assert fk.referred_table.lower() == "teams"
    assert fk.referred_columns == ["team_id"]
    assert fk.on_delete == "CASCADE"


def test_live_schema_inspection_with_engine():
    # Test using an in-memory SQLite engine
    engine = create_engine("sqlite:///:memory:")
    meta = MetaData()
    companies = Table(
        "companies",
        meta,
        Column("id", Integer, primary_key=True),
        Column("title", String(100), nullable=False),
    )
    positions = Table(
        "positions",
        meta,
        Column("pos_id", Integer, primary_key=True),
        Column("company_id", Integer, ForeignKey("companies.id", ondelete="SET NULL")),
        Column("role", String(100), nullable=False),
    )
    meta.create_all(engine)

    profile = ConnectionProfile(
        name="test-engine-live",
        dialect=Dialect.SQLITE,
        database=":memory:",
    )
    source = LiveDatabaseSchemaSource(profile, engine=engine)
    result = source.discover()

    assert not result.has_errors
    assert result.schema.get_table("companies") is not None
    assert result.schema.get_table("positions") is not None

    pos = result.schema.get_table("positions")
    assert pos is not None
    assert len(pos.foreign_keys) == 1
    fk = pos.foreign_keys[0]
    assert fk.constrained_columns == ["company_id"]
    assert fk.referred_table.lower() == "companies"
    assert fk.referred_columns == ["id"]


def test_live_schema_composite_keys_and_unique_constraints():
    """Verify composite primary keys, composite unique constraints, and foreign keys."""
    engine = create_engine("sqlite:///:memory:")
    meta = MetaData()
    tenants = Table(
        "tenants",
        meta,
        Column("tenant_id", Integer, primary_key=True),
        Column("tenant_code", String(50), nullable=False, unique=True),
    )
    user_roles = Table(
        "user_roles",
        meta,
        Column("tenant_id", Integer, primary_key=True),
        Column("user_id", Integer, primary_key=True),
        Column("role_name", String(50), nullable=False),
        ForeignKeyConstraint(["tenant_id"], ["tenants.tenant_id"], ondelete="CASCADE"),
        UniqueConstraint("user_id", "role_name", name="uq_user_role"),
    )
    meta.create_all(engine)

    profile = ConnectionProfile(
        name="test-composite-live",
        dialect=Dialect.SQLITE,
        database=":memory:",
    )
    source = LiveDatabaseSchemaSource(profile, engine=engine)
    result = source.discover()

    assert not result.has_errors
    ur = result.schema.get_table("user_roles")
    assert ur is not None
    # Verify composite primary key
    assert ur.primary_key == ["tenant_id", "user_id"]
    # Verify composite unique constraint
    assert any(uq.columns == ["user_id", "role_name"] for uq in ur.unique_constraints)
    # Verify foreign key
    assert len(ur.foreign_keys) == 1
    assert ur.foreign_keys[0].constrained_columns == ["tenant_id"]
    assert ur.foreign_keys[0].referred_table.lower() == "tenants"


def test_live_schema_inspection_failure_error_hint():
    """Verify that connection failure during inspection produces a classified error with hint."""
    profile = ConnectionProfile(
        name="unreachable-profile",
        dialect=Dialect.POSTGRES,
        host="127.0.0.1",
        port=59998,
        database="nonexistent",
        username="user",
    )
    source = LiveDatabaseSchemaSource(profile, password="pass")
    with pytest.raises(DatabaseAccessError) as exc_info:
        source.discover()

    assert exc_info.value.details is not None
    assert (
        "server is unreachable" in exc_info.value.details
        or "driver is compatible" in exc_info.value.details
        or "timed out" in exc_info.value.details
    )
