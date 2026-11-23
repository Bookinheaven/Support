"""Unit tests for SQL Server schema introspection."""

from unittest.mock import MagicMock, patch
import pytest

pytest.importorskip("mssql_python")

from support.domain.profiles import SQLServerConnection
from support.domain.schema import TableType
from support.infrastructure.sqlserver import SQLServerConnector


def test_sqlserver_schema_introspection() -> None:
    connector = SQLServerConnector()
    conn_config = SQLServerConnection(database="enterprise", username="sa")

    mock_conn = MagicMock()
    mock_cursor = MagicMock()
    mock_conn.cursor.return_value = mock_cursor

    mock_cursor.fetchall.side_effect = [
        # 1. Schemas
        [("dbo",), ("sales",)],
        # 2. Tables & Views
        [
            ("dbo", "customers", "USER_TABLE"),
            ("dbo", "v_customers", "VIEW"),
        ],
        # 3. Columns
        [
            ("dbo", "customers", "id", "int", "NO", None, 1),
            ("dbo", "customers", "name", "nvarchar(100)", "YES", None, 2),
            ("dbo", "v_customers", "id", "int", "YES", None, 1),
        ],
        # 4. PK & Unique
        [
            ("PK_customers", "PRIMARY_KEY_CONSTRAINT", "dbo", "customers", "id"),
        ],
        # 5. Foreign Keys
        [],
        # 6. Indexes
        [
            ("dbo", "customers", "PK_customers", "id", 1, 1, "CLUSTERED", 0, 0, None),
            ("dbo", "customers", "IX_customers_email", "email", 1, 0, "NONCLUSTERED", 0, 1, "([status]='active')"),
            ("dbo", "customers", "IX_customers_email", "name", 0, 0, "NONCLUSTERED", 1, 0, "([status]='active')"),
        ],
        # 7. Check constraints
        [],
    ]

    with patch("mssql_python.connect", return_value=mock_conn):
        schema = connector.introspect_schema(conn_config, password="pass")

    assert schema.database_name == "enterprise"
    assert schema.engine == "SQL Server"
    assert "dbo" in schema.schemas
    assert len(schema.tables) == 2

    cust = schema.get_table("customers")
    assert cust is not None
    assert cust.table_type == TableType.TABLE
    assert len(cust.columns) == 2
    assert cust.primary_key is not None
    assert cust.primary_key.columns == ("id",)

    # Verify index fidelity
    filtered_idx = next(i for i in cust.indexes if i.name == "IX_customers_email")
    assert filtered_idx.columns == ("email",)
    assert filtered_idx.column_orders == ("DESC",)
    assert filtered_idx.included_columns == ("name",)
    assert filtered_idx.unique is True
    assert filtered_idx.filter_condition == "([status]='active')"
