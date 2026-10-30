"""Tests for database ER diagram generation, Mermaid syntax, and workflows."""

from io import StringIO
from pathlib import Path
from unittest.mock import MagicMock, patch
import pytest
from typer.testing import CliRunner

from support.cli.db_cmds import db_app
from support.core.dialect import Dialect
from support.core.exceptions import SupportError
from support.db.models import ConnectionProfile
from support.tools.selector import interpret_and_select_tool
from support.workspace.state import WorkspaceSessionState
from support.schema.diagram import (
    generate_mermaid_er,
    get_default_diagram_path,
    resolve_selected_tables_with_paths,
    sanitize_mermaid_identifier,
    sanitize_type_for_mermaid,
    save_diagram_file,
)
from support.schema.models import (
    ColumnSchema,
    DatabaseSchema,
    ForeignKeySchema,
    TableSchema,
    UniqueConstraintSchema,
)


@pytest.fixture
def relational_schema():
    """Build a rich relational schema with 1-to-many, 1-to-1, composite, and standalone tables."""
    schema = DatabaseSchema(dialect=Dialect.POSTGRES, database_name="ecommerce")

    # 1. users table
    users = TableSchema(
        name="users",
        primary_key=["id"],
        columns=[
            ColumnSchema(name="id", data_type="INTEGER", is_primary_key=True, nullable=False),
            ColumnSchema(name="username", data_type="VARCHAR(50)", nullable=False, is_unique=True),
            ColumnSchema(name="email", data_type="VARCHAR(100)", nullable=False),
        ],
    )
    schema.tables["users"] = users

    # 2. user_profiles table (1-to-1 with users via unique FK on user_id)
    user_profiles = TableSchema(
        name="user_profiles",
        primary_key=["id"],
        columns=[
            ColumnSchema(name="id", data_type="INTEGER", is_primary_key=True, nullable=False),
            ColumnSchema(name="user_id", data_type="INTEGER", nullable=False, is_unique=True),
            ColumnSchema(name="bio", data_type="TEXT", nullable=True),
        ],
        foreign_keys=[
            ForeignKeySchema(
                name="fk_profiles_users",
                constrained_columns=["user_id"],
                referred_table="users",
                referred_columns=["id"],
            )
        ],
    )
    schema.tables["user_profiles"] = user_profiles

    # 3. orders table (1-to-many from users, non-nullable FK)
    orders = TableSchema(
        name="orders",
        primary_key=["id"],
        columns=[
            ColumnSchema(name="id", data_type="INTEGER", is_primary_key=True, nullable=False),
            ColumnSchema(name="user_id", data_type="INTEGER", nullable=False),
            ColumnSchema(name="total_amount", data_type="NUMERIC(10, 2)", nullable=False),
        ],
        foreign_keys=[
            ForeignKeySchema(
                name="fk_orders_users",
                constrained_columns=["user_id"],
                referred_table="users",
                referred_columns=["id"],
            )
        ],
    )
    schema.tables["orders"] = orders

    # 4. order_items table (composite foreign key)
    order_items = TableSchema(
        name="order_items",
        primary_key=["order_id", "line_no"],
        columns=[
            ColumnSchema(name="order_id", data_type="INTEGER", is_primary_key=True, nullable=False),
            ColumnSchema(name="line_no", data_type="INTEGER", is_primary_key=True, nullable=False),
            ColumnSchema(name="product_name", data_type="VARCHAR(100)", nullable=False),
        ],
        foreign_keys=[
            ForeignKeySchema(
                name="fk_items_orders",
                constrained_columns=["order_id"],
                referred_table="orders",
                referred_columns=["id"],
            )
        ],
    )
    schema.tables["order_items"] = order_items

    # 5. audit_logs (standalone table without foreign keys)
    audit_logs = TableSchema(
        name="audit_logs",
        primary_key=["id"],
        columns=[
            ColumnSchema(name="id", data_type="BIGINT", is_primary_key=True, nullable=False),
            ColumnSchema(name="action", data_type="VARCHAR(50)", nullable=False),
        ],
    )
    schema.tables["audit_logs"] = audit_logs

    return schema


def test_sanitize_mermaid_identifier():
    """Verify table and column names with spaces, dashes, dots, or leading digits are normalized."""
    assert sanitize_mermaid_identifier("users") == "users"
    assert sanitize_mermaid_identifier("order-items") == "order_items"
    assert sanitize_mermaid_identifier("public.accounts") == "public_accounts"
    assert sanitize_mermaid_identifier("user details") == "user_details"
    assert sanitize_mermaid_identifier("123orders") == "t_123orders"
    assert sanitize_mermaid_identifier("") == "unnamed"


def test_sanitize_type_for_mermaid():
    """Verify SQL data types are normalized to valid single-word Mermaid attribute types."""
    assert sanitize_type_for_mermaid("VARCHAR(255)") == "varchar"
    assert sanitize_type_for_mermaid("INTEGER") == "int"
    assert sanitize_type_for_mermaid("BIGINT") == "bigint"
    assert sanitize_type_for_mermaid("NUMERIC(10, 2)") == "numeric"
    assert sanitize_type_for_mermaid("TIMESTAMP WITH TIME ZONE") == "timestamptz"
    assert sanitize_type_for_mermaid("BOOLEAN") == "boolean"
    assert sanitize_type_for_mermaid("UNIQUEIDENTIFIER") == "uuid"
    assert sanitize_type_for_mermaid("") == "string"


def test_generate_mermaid_er_relational(relational_schema):
    """Verify full ER diagram generates valid Mermaid syntax with PK, FK, and relationships."""
    code = generate_mermaid_er(relational_schema)

    assert code.startswith("erDiagram\n")
    # All 5 tables rendered
    assert "    users {" in code
    assert "    user_profiles {" in code
    assert "    orders {" in code
    assert "    order_items {" in code
    assert "    audit_logs {" in code

    # Keys indicators
    assert "int id PK" in code
    assert "int user_id FK" in code

    # 1-to-1 cardinality for user_profiles (unique FK, non-nullable)
    assert 'users ||--|| user_profiles : "user_id"' in code

    # 1-to-many cardinality for orders (non-nullable FK)
    assert 'users ||--|{ orders : "user_id"' in code
    assert 'orders ||--|{ order_items : "order_id"' in code


def test_nullable_foreign_key_cardinality():
    """Verify nullable foreign keys produce zero-or-more (|o or o{) cardinality."""
    schema = DatabaseSchema(dialect=Dialect.POSTGRES, database_name="testdb")
    schema.tables["departments"] = TableSchema(
        name="departments",
        primary_key=["id"],
        columns=[ColumnSchema(name="id", data_type="INT", is_primary_key=True)],
    )
    schema.tables["employees"] = TableSchema(
        name="employees",
        primary_key=["id"],
        columns=[
            ColumnSchema(name="id", data_type="INT", is_primary_key=True),
            ColumnSchema(name="dept_id", data_type="INT", nullable=True),  # Nullable FK
        ],
        foreign_keys=[
            ForeignKeySchema(
                constrained_columns=["dept_id"],
                referred_table="departments",
                referred_columns=["id"],
            )
        ],
    )

    code = generate_mermaid_er(schema)
    assert 'departments ||--o{ employees : "dept_id"' in code


def test_composite_foreign_key():
    """Verify composite foreign keys include multiple constrained columns in relationship label."""
    schema = DatabaseSchema(dialect=Dialect.SQLITE, database_name="warehouse")
    schema.tables["stores"] = TableSchema(
        name="stores",
        primary_key=["market_id", "store_id"],
        columns=[
            ColumnSchema(name="market_id", data_type="INT", is_primary_key=True),
            ColumnSchema(name="store_id", data_type="INT", is_primary_key=True),
        ],
    )
    schema.tables["staff"] = TableSchema(
        name="staff",
        primary_key=["id"],
        columns=[
            ColumnSchema(name="id", data_type="INT", is_primary_key=True),
            ColumnSchema(name="market_id", data_type="INT", nullable=False),
            ColumnSchema(name="store_id", data_type="INT", nullable=False),
        ],
        foreign_keys=[
            ForeignKeySchema(
                constrained_columns=["market_id", "store_id"],
                referred_table="stores",
                referred_columns=["market_id", "store_id"],
            )
        ],
    )

    code = generate_mermaid_er(schema)
    assert 'stores ||--|{ staff : "market_id, store_id"' in code


def test_tables_without_relationships_render_cleanly():
    """Verify standalone tables without foreign keys render without syntax errors."""
    schema = DatabaseSchema(dialect=Dialect.MYSQL, database_name="logs")
    schema.tables["events"] = TableSchema(
        name="events",
        primary_key=["id"],
        columns=[ColumnSchema(name="id", data_type="INT", is_primary_key=True)],
    )
    code = generate_mermaid_er(schema)
    assert "    events {" in code
    assert "--" not in code  # No relationship lines


def test_empty_schema_handling():
    """Verify empty schema outputs valid comment without crashing."""
    schema = DatabaseSchema(dialect=Dialect.POSTGRES, database_name="empty_db")
    code = generate_mermaid_er(schema)
    assert code.startswith("erDiagram\n")
    assert "No tables found" in code


def test_selected_tables_with_junction_path_preservation(relational_schema):
    """Verify filtering by subset preserves relationship paths and junction tables."""
    # Request only users and order_items
    filtered = resolve_selected_tables_with_paths(relational_schema, ["users", "order_items"])
    filtered_names = {t.name.lower() for t in filtered}

    # orders is the junction bridging users and order_items, so it must be included
    assert "users" in filtered_names
    assert "order_items" in filtered_names
    assert "orders" in filtered_names
    # Unrelated tables are omitted
    assert "audit_logs" not in filtered_names


def test_save_diagram_file_and_overwrite_protection(tmp_path):
    """Verify diagram file saving, overwrite protection, and optional HTML viewer generation."""
    code = "erDiagram\n    users {\n        int id PK\n    }\n"
    out_file = tmp_path / "diagram.mmd"

    # 1. Initial save
    mmd_p, html_p = save_diagram_file(code, output_path=out_file, overwrite=False, generate_html=True)
    assert mmd_p.exists()
    assert mmd_p.read_text(encoding="utf-8") == code
    assert html_p is not None and html_p.exists()
    assert "mermaid.min.js" in html_p.read_text(encoding="utf-8")

    # 2. Overwrite error without flag
    with pytest.raises(FileExistsError):
        save_diagram_file(code, output_path=out_file, overwrite=False)

    # 3. Successful overwrite with flag
    save_diagram_file(code + "    %% updated\n", output_path=out_file, overwrite=True)
    assert "%% updated" in out_file.read_text(encoding="utf-8")


def test_mongodb_relational_diagram_refusal():
    """Verify MongoDB schema raises SupportError rather than inventing relational foreign keys."""
    mongo_schema = DatabaseSchema(dialect=Dialect.MONGODB, database_name="mongo_store")
    mongo_schema.tables["customers"] = TableSchema(
        name="customers",
        columns=[ColumnSchema(name="_id", data_type="OBJECTID")],
    )

    with pytest.raises(SupportError) as exc_info:
        generate_mermaid_er(mongo_schema)
    assert "MongoDB" in str(exc_info.value)
    assert "relational foreign keys" in str(exc_info.value)


def test_cli_db_diagram_command(tmp_path):
    """Verify 'support db diagram <profile>' CLI command generates .mmd file."""
    mock_service = MagicMock()
    mock_profile = ConnectionProfile(
        name="athena",
        dialect=Dialect.POSTGRES,
        host="localhost",
        database="testdb",
        username="postgres",
    )
    mock_service.manager.get_profile_with_password.return_value = (mock_profile, "secret123")

    mock_schema = DatabaseSchema(dialect=Dialect.POSTGRES, database_name="testdb")
    mock_schema.tables["users"] = TableSchema(
        name="users",
        primary_key=["id"],
        columns=[ColumnSchema(name="id", data_type="INT", is_primary_key=True)],
    )

    out_file = tmp_path / "custom.mmd"

    with patch("support.cli.db_cmds.DatabaseService", return_value=mock_service), \
         patch("support.schema.sources.live_db.LiveDatabaseSchemaSource.discover") as mock_disc:
        mock_disc.return_value = MagicMock(schema=mock_schema)
        runner = CliRunner()
        res = runner.invoke(db_app, ["diagram", "athena", "-o", str(out_file), "-y"])
        assert res.exit_code == 0
        assert "Generated Mermaid ER diagram" in res.output
        assert out_file.exists()
        assert "users {" in out_file.read_text(encoding="utf-8")
        # Ensure password is never in output file
        assert "secret123" not in out_file.read_text(encoding="utf-8")


def test_cli_mongodb_informational_notice():
    """Verify CLI gives a polite notice when diagramming a MongoDB profile."""
    mock_service = MagicMock()
    mock_profile = ConnectionProfile(
        name="mongodev",
        dialect=Dialect.MONGODB,
        host="localhost",
        database="testdb",
        username="mongo",
    )
    mock_service.manager.get_profile_with_password.return_value = (mock_profile, None)

    with patch("support.cli.db_cmds.DatabaseService", return_value=mock_service):
        runner = CliRunner()
        res = runner.invoke(db_app, ["diagram", "mongodev"])
        assert res.exit_code == 0
        assert "MongoDB" in res.output
        assert "Relational ER diagrams" in res.output
        assert "support db inspect" in res.output


def test_tool_selection_for_diagram_requests():
    """Verify natural-language diagram requests select generate_diagram tool."""
    state = WorkspaceSessionState()
    prompts = [
        ("Generate an ER diagram.", []),
        ("Show the database relationships.", []),
        ("Create a diagram of the users and tasks tables.", ["users", "tasks"]),
        ("Show ER diagram for orders", ["orders"]),
    ]

    for q, expected_tables in prompts:
        call = interpret_and_select_tool(q, state=state)
        assert call.tool == "generate_diagram", f"Failed for query: {q}"
        for tbl in expected_tables:
            assert tbl in call.arguments.get("tables", [])
