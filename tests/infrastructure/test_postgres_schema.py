"""Unit tests for PostgreSQL schema introspection."""

from unittest.mock import MagicMock, patch
import pytest

pytest.importorskip("psycopg")

from support.application.errors import ObjectNotFoundError
from support.domain.profiles import PostgresConnection
from support.domain.schema import TableType
from support.infrastructure.postgres import PostgresConnector


def test_postgres_schema_introspection() -> None:
    connector = PostgresConnector()
    conn_config = PostgresConnection(database="appdb", username="postgres", ssl_mode="prefer")

    mock_conn = MagicMock()
    mock_cursor = MagicMock()
    mock_conn.cursor.return_value.__enter__.return_value = mock_cursor

    # Query 1: Schemas
    # Query 2: Tables/Views
    # Query 3: Columns
    # Query 4: Constraints (PK, Unique)
    # Query 5: Foreign Keys
    # Query 6: Check Constraints
    # Query 7: Indexes
    mock_cursor.fetchall.side_effect = [
        # 1. Schemas
        [("public",), ("analytics",)],
        # 2. Tables
        [
            ("public", "users", "BASE TABLE"),
            ("public", "v_users", "VIEW"),
        ],
        # 3. Columns
        [
            ("public", "users", "id", "integer", "NO", None, 1),
            ("public", "users", "name", "varchar", "YES", None, 2),
            ("public", "v_users", "id", "integer", "YES", None, 1),
        ],
        # 4. PK and Unique Constraints
        [
            ("pk_users", "PRIMARY KEY", "public", "users", "id"),
        ],
        # 5. Foreign Keys
        [],
        # 6. Check Constraints
        [],
        # 7. Indexes
        [
            ("public", "users", "idx_users_id", 'CREATE UNIQUE INDEX idx_users_id ON public.users USING btree (id)'),
        ],
    ]

    with patch("psycopg.connect", return_value=mock_conn):
        schema = connector.introspect_schema(conn_config, password="pass")

    assert schema.database_name == "appdb"
    assert schema.engine == "PostgreSQL"
    assert schema.schemas == ("public", "analytics")
    assert len(schema.tables) == 2

    users = schema.get_table("users")
    assert users is not None
    assert users.table_type == TableType.TABLE
    assert len(users.columns) == 2
    assert users.primary_key is not None
    assert users.primary_key.columns == ("id",)
    assert len(users.indexes) == 1
    assert users.indexes[0].unique is True

    view = schema.get_table("v_users")
    assert view is not None
    assert view.table_type == TableType.VIEW
