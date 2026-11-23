"""Unit tests for schema presentation rendering."""

import io
from rich.console import Console

from support.cli.presentation import (
    render_object_description,
    render_relationships,
    render_schema_overview,
    render_tables,
)
from support.domain.schema import (
    ColumnMetadata,
    ConstraintMetadata,
    DatabaseSchema,
    ForeignKeyMetadata,
    IndexMetadata,
    TableMetadata,
    TableType,
)


def _capture_output(func, *args, **kwargs) -> str:
    buf = io.StringIO()
    console = Console(file=buf, no_color=True, width=120)
    func(console, *args, **kwargs)
    return buf.getvalue()


def test_render_tables_relational() -> None:
    t1 = TableMetadata(name="users", table_type=TableType.TABLE)
    t2 = TableMetadata(name="orders", table_type=TableType.TABLE)
    v1 = TableMetadata(name="active_users", table_type=TableType.VIEW)
    schema = DatabaseSchema(
        database_name="appdb",
        engine="postgresql",
        tables=(t1, t2, v1),
    )

    out = _capture_output(render_tables, schema)
    assert "Tables in appdb:" in out
    assert "users" in out
    assert "orders" in out
    assert "Views in appdb:" in out
    assert "active_users" in out
    assert "2 tables · 1 view" in out


def test_render_tables_relational_empty() -> None:
    schema = DatabaseSchema(database_name="emptydb", engine="sqlite")
    out = _capture_output(render_tables, schema)
    assert "No tables or views found in database 'emptydb'." in out


def test_render_tables_mongodb() -> None:
    c1 = TableMetadata(name="customers", table_type=TableType.COLLECTION)
    c2 = TableMetadata(name="logs", table_type=TableType.COLLECTION)
    schema = DatabaseSchema(database_name="analytics", engine="mongodb", tables=(c1, c2))

    out = _capture_output(render_tables, schema)
    assert "Collections in analytics:" in out
    assert "customers" in out
    assert "logs" in out
    assert "2 collections" in out


def test_render_schema_overview_relational() -> None:
    t1 = TableMetadata(name="users")
    fk = ForeignKeyMetadata("fk1", "orders", ("u_id",), "users", ("id",))
    schema = DatabaseSchema(
        database_name="prod",
        engine="postgresql",
        schemas=("public", "audit"),
        tables=(t1,),
        relationships=(fk,),
    )
    out = _capture_output(render_schema_overview, schema)
    assert "Database:      prod" in out
    assert "Engine:        PostgreSQL" in out
    assert "Schemas:       2" in out
    assert "Tables:        1" in out
    assert "Relationships: 1" in out


def test_render_schema_overview_mongodb() -> None:
    c1 = TableMetadata(name="coll1", indexes=(IndexMetadata(name="_id_", columns=("_id",)),))
    schema = DatabaseSchema(
        database_name="testmongo",
        engine="mongodb",
        tables=(c1,),
    )
    out = _capture_output(render_schema_overview, schema)
    assert "Database:    testmongo" in out
    assert "Engine:      MongoDB" in out
    assert "Collections: 1" in out
    assert "Indexes:     1" in out


def test_render_object_description_relational() -> None:
    cols = (
        ColumnMetadata("id", "INTEGER", nullable=False),
        ColumnMetadata("email", "VARCHAR(255)", nullable=False),
        ColumnMetadata("status", "VARCHAR(20)", nullable=True, default="'active'"),
    )
    pk = ConstraintMetadata("pk_u", ("id",), "PRIMARY KEY")
    unique = (ConstraintMetadata("uq_email", ("email",), "UNIQUE"),)
    fk = (
        ForeignKeyMetadata(
            "fk_u_role",
            "users",
            ("role_id",),
            "roles",
            ("id",),
            on_delete="CASCADE",
        ),
    )
    chk = (ConstraintMetadata("chk_status", ("status",), "CHECK", check_clause="status IN ('active', 'inactive')"),)
    idx = (
        IndexMetadata(
            "idx_users_email",
            ("email",),
            column_orders=("DESC",),
            included_columns=("created_at",),
            unique=True,
            method="BTREE",
            filter_condition="status = 'active'",
        ),
    )

    table = TableMetadata(
        name="users",
        schema_name="public",
        columns=cols,
        primary_key=pk,
        unique_constraints=unique,
        foreign_keys=fk,
        check_constraints=chk,
        indexes=idx,
    )

    out = _capture_output(render_object_description, table, "postgresql")
    assert "Table:       public.users" in out
    assert "Columns:" in out
    assert "id" in out
    assert "INTEGER" in out
    assert "NOT NULL" in out
    assert "PK" in out
    assert "DEFAULT 'active'" in out
    assert "Primary Key:" in out
    assert "(id)" in out
    assert "Unique Constraints:" in out
    assert "(email)" in out
    assert "Foreign Keys:" in out
    assert "users.role_id -> roles.id ON DELETE CASCADE" in out
    assert "Check Constraints:" in out
    assert "status IN ('active', 'inactive')" in out
    assert "Indexes:" in out
    assert "idx_users_email" in out
    assert "(email DESC) INCLUDE (created_at) WHERE status = 'active'" in out


def test_render_object_description_mongodb() -> None:
    table = TableMetadata(
        name="events",
        table_type=TableType.COLLECTION,
        options={"capped": True, "size": 5242880},
        validator={"$jsonSchema": {"required": ["ts"]}},
        indexes=(
            IndexMetadata("_id_", ("_id",), unique=True),
            IndexMetadata("ts_1", ("ts",)),
        ),
    )

    out = _capture_output(render_object_description, table, "mongodb")
    assert "Collection:  events" in out
    assert "Engine:      MongoDB" in out
    assert "Options:" in out
    assert "capped: True" in out
    assert "Validator:" in out
    assert "$jsonSchema" in out
    assert "Indexes:" in out
    assert "_id_" in out
    assert "ts_1" in out


def test_render_relationships_relational() -> None:
    fk1 = ForeignKeyMetadata(
        "fk1",
        "orders",
        ("user_id",),
        "users",
        ("id",),
        on_delete="CASCADE",
    )
    schema = DatabaseSchema(
        database_name="shop",
        engine="mysql",
        relationships=(fk1,),
    )
    out = _capture_output(render_relationships, schema)
    assert "Foreign-key relationships in shop:" in out
    assert "orders.user_id" in out
    assert "-> users.id (ON DELETE CASCADE)" in out
    assert "1 relationship" in out


def test_render_relationships_relational_empty() -> None:
    schema = DatabaseSchema(database_name="empty", engine="sqlite")
    out = _capture_output(render_relationships, schema)
    assert "No declared foreign key relationships found in database 'empty'." in out


def test_render_relationships_mongodb() -> None:
    schema = DatabaseSchema(database_name="mongo", engine="mongodb")
    out = _capture_output(render_relationships, schema)
    assert "MongoDB collections do not define relational foreign-key constraints." in out
