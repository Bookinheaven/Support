"""Unit tests for MySQL / MariaDB schema introspection."""

from unittest.mock import MagicMock, patch
import pytest

pytest.importorskip("pymysql")

from support.domain.profiles import MySQLConnection
from support.domain.schema import TableType
from support.infrastructure.mysql_connector import MySQLCompatibleConnector


def test_mysql_schema_introspection() -> None:
    connector = MySQLCompatibleConnector()
    conn_config = MySQLConnection(database="shop", username="root", ssl_mode="disabled")

    mock_conn = MagicMock()
    mock_cursor = MagicMock()
    mock_conn.cursor.return_value.__enter__.return_value = mock_cursor

    # Query 1: Tables
    # Query 2: Columns
    # Query 3: Constraints (PK, Unique)
    # Query 4: Foreign Keys
    # Query 5: Check Constraints (or try/except)
    # Query 6: Indexes
    mock_cursor.fetchall.side_effect = [
        # Tables
        [
            ("products", "BASE TABLE"),
            ("active_products", "VIEW"),
        ],
        # Columns
        [
            ("products", "id", "int", "NO", None, 1),
            ("products", "name", "varchar(100)", "NO", None, 2),
            ("active_products", "id", "int", "YES", None, 1),
        ],
        # Constraints
        [
            ("PRIMARY", "PRIMARY KEY", "products", "id"),
        ],
        # 4. Foreign Keys
        [],
        # 5. Indexes
        [
            ("products", "PRIMARY", "id", 0, "BTREE", "A"),
            ("products", "idx_name_desc", "name", 1, "BTREE", "D"),
        ],
        # 6. Check constraints
        [],
    ]

    with patch("pymysql.connect", return_value=mock_conn):
        schema = connector.introspect_schema(conn_config, password="pass")

    assert schema.database_name == "shop"
    assert schema.engine == "MySQL"
    assert len(schema.tables) == 2

    prod = schema.get_table("products")
    assert prod is not None
    assert prod.table_type == TableType.TABLE
    assert len(prod.columns) == 2
    assert prod.primary_key is not None
    assert prod.primary_key.columns == ("id",)

    idx_desc = next(i for i in prod.indexes if i.name == "idx_name_desc")
    assert idx_desc.columns == ("name",)
    assert idx_desc.column_orders == ("DESC",)
