"""Unit tests for database tools and registry execution."""

from pathlib import Path
import sqlite3
from unittest.mock import MagicMock
import pytest

from support.application.connection import ConnectionTestWorkflow
from support.application.schema import SchemaIntrospectionWorkflow
from support.application.tools import ToolRegistry, APPROVED_DATABASE_TOOLS
from support.domain.profiles import DatabaseProfile, SQLiteConnection, MongoDBConnection
from support.domain.schema import DatabaseSchema, TableMetadata, TableType, ColumnMetadata, ForeignKeyMetadata, IndexMetadata


def _create_sqlite_db(db_path: Path) -> None:
    conn = sqlite3.connect(str(db_path))
    with conn:
        conn.execute("CREATE TABLE users (id INTEGER PRIMARY KEY, username TEXT NOT NULL, email TEXT UNIQUE);")
        conn.execute(
            """
            CREATE TABLE orders (
                id INTEGER PRIMARY KEY,
                user_id INTEGER NOT NULL,
                total REAL NOT NULL,
                FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
            );
            """
        )
        conn.execute("CREATE VIEW active_users AS SELECT id, username FROM users;")
        conn.execute("CREATE INDEX idx_orders_total ON orders (total DESC);")
    conn.close()


@pytest.fixture
def test_sqlite_environment(tmp_path: Path):
    db_file = tmp_path / "app.db"
    _create_sqlite_db(db_file)

    conn_config = SQLiteConnection(database_path=str(db_file))
    profile = DatabaseProfile(name="test-sqlite", connection=conn_config)

    repo = MagicMock()
    repo.get_by_name.return_value = profile

    connector = MagicMock()
    from support.infrastructure.sqlite_connector import SQLiteConnector
    sqlite_conn = SQLiteConnector()
    connector.introspect_schema.side_effect = sqlite_conn.introspect_schema
    connector.introspect_table.side_effect = sqlite_conn.introspect_table
    connector.test_connection.side_effect = sqlite_conn.test_connection

    schema_wf = SchemaIntrospectionWorkflow(repository=repo, introspector=connector)
    conn_wf = ConnectionTestWorkflow(repository=repo, connector=connector)
    registry = ToolRegistry(schema_workflow=schema_wf, connection_workflow=conn_wf)

    return registry, "test-sqlite"


def test_tool_registry_approved_tools_count(test_sqlite_environment) -> None:
    registry, _ = test_sqlite_environment
    tool_defs = registry.get_tool_definitions()
    assert len(tool_defs) == 5
    names = {t.name for t in tool_defs}
    assert names == {
        "list_database_objects",
        "get_database_overview",
        "describe_object",
        "get_relationships",
        "get_connection_status",
    }


def test_tool_no_active_profile_error(test_sqlite_environment) -> None:
    registry, _ = test_sqlite_environment
    res = registry.execute_tool("list_database_objects", {}, active_profile=None)
    assert res.is_error is True
    assert "No active database connection" in res.data["error"]


def test_tool_unknown_tool_error(test_sqlite_environment) -> None:
    registry, profile = test_sqlite_environment
    res = registry.execute_tool("execute_sql", {"query": "SELECT * FROM users"}, active_profile=profile)
    assert res.is_error is True
    assert "Unknown tool" in res.data["error"]


def test_tool_list_database_objects_sqlite(test_sqlite_environment) -> None:
    registry, profile = test_sqlite_environment
    res = registry.execute_tool("list_database_objects", {"object_type": "all"}, active_profile=profile)
    assert res.is_error is False
    assert "users" in res.data["tables"]
    assert "orders" in res.data["tables"]
    assert "active_users" in res.data["views"]
    assert res.data["table_count"] == 2
    assert res.data["view_count"] == 1
    assert isinstance(res.raw_object, DatabaseSchema)


def test_tool_get_database_overview_sqlite(test_sqlite_environment) -> None:
    registry, profile = test_sqlite_environment
    res = registry.execute_tool("get_database_overview", {}, active_profile=profile)
    assert res.is_error is False
    assert res.data["engine"] == "SQLite"
    assert res.data["tables_count"] == 2
    assert res.data["views_count"] == 1
    assert res.data["relationships_count"] == 1


def test_tool_describe_object_success(test_sqlite_environment) -> None:
    registry, profile = test_sqlite_environment
    res = registry.execute_tool("describe_object", {"object_name": "users"}, active_profile=profile)
    assert res.is_error is False
    assert res.data["name"] == "users"
    col_names = [c["name"] for c in res.data["columns"]]
    assert "id" in col_names
    assert "username" in col_names
    assert "email" in col_names
    assert res.data["primary_key"] == ["id"]
    assert isinstance(res.raw_object, TableMetadata)


def test_tool_describe_object_missing_name_parameter(test_sqlite_environment) -> None:
    registry, profile = test_sqlite_environment
    res = registry.execute_tool("describe_object", {}, active_profile=profile)
    assert res.is_error is True
    assert "required" in res.data["error"]


def test_tool_describe_object_not_found(test_sqlite_environment) -> None:
    registry, profile = test_sqlite_environment
    res = registry.execute_tool("describe_object", {"object_name": "nonexistent"}, active_profile=profile)
    assert res.is_error is True
    assert "does not exist" in res.data["error"]


def test_tool_get_relationships_sqlite(test_sqlite_environment) -> None:
    registry, profile = test_sqlite_environment
    res = registry.execute_tool("get_relationships", {}, active_profile=profile)
    assert res.is_error is False
    assert len(res.data["relationships"]) == 1
    rel = res.data["relationships"][0]
    assert rel["source_table"] == "orders"
    assert rel["target_table"] == "users"
    assert rel["on_delete"] == "CASCADE"


def test_tool_get_relationships_filtered_by_table(test_sqlite_environment) -> None:
    registry, profile = test_sqlite_environment
    # Filter matching orders
    res1 = registry.execute_tool("get_relationships", {"table_name": "orders"}, active_profile=profile)
    assert len(res1.data["relationships"]) == 1

    # Filter with non-related table
    res2 = registry.execute_tool("get_relationships", {"table_name": "unrelated"}, active_profile=profile)
    assert len(res2.data["relationships"]) == 0


def test_tool_get_connection_status_safe(test_sqlite_environment) -> None:
    registry, profile = test_sqlite_environment
    res = registry.execute_tool("get_connection_status", {}, active_profile=profile)
    assert res.is_error is False
    assert res.data["profile_name"] == profile
    assert res.data["database_type"] == "sqlite"
    assert "password" not in res.data


def test_tool_mongodb_collection_and_no_fks() -> None:
    # Setup mock MongoDB
    mongo_conn = MongoDBConnection(uri="mongodb://localhost:27017", database="analytics")
    profile = DatabaseProfile(name="mongo-prof", connection=mongo_conn)

    repo = MagicMock()
    repo.get_by_name.return_value = profile

    c1 = TableMetadata(
        name="events",
        table_type=TableType.COLLECTION,
        options={"capped": True},
        indexes=(IndexMetadata(name="_id_", columns=("_id",)),),
    )
    mock_schema = DatabaseSchema(database_name="analytics", engine="MongoDB", tables=(c1,))

    introspector = MagicMock()
    introspector.introspect_schema.return_value = mock_schema
    introspector.introspect_table.return_value = c1

    schema_wf = SchemaIntrospectionWorkflow(repository=repo, introspector=introspector)
    conn_wf = ConnectionTestWorkflow(repository=repo, connector=introspector)
    registry = ToolRegistry(schema_workflow=schema_wf, connection_workflow=conn_wf)

    # List collections
    res_list = registry.execute_tool("list_database_objects", {}, active_profile="mongo-prof")
    assert res_list.data["engine"] == "MongoDB"
    assert res_list.data["collections"] == ["events"]

    # Overview
    res_over = registry.execute_tool("get_database_overview", {}, active_profile="mongo-prof")
    assert res_over.data["collections_count"] == 1
    assert res_over.data["total_indexes"] == 1

    # Relationships report no foreign keys
    res_rel = registry.execute_tool("get_relationships", {}, active_profile="mongo-prof")
    assert "do not define relational foreign-key constraints" in res_rel.data["message"]
    assert res_rel.data["relationships"] == []
