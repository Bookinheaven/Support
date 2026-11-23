"""Unit tests for domain schema models."""

import json
import pytest
from support.application.errors import AmbiguousObjectNameError
from support.domain.schema import (
    ColumnMetadata,
    ConstraintMetadata,
    DatabaseSchema,
    ForeignKeyMetadata,
    IndexMetadata,
    ReadOnlyDict,
    TableMetadata,
    TableType,
    parse_object_name,
    unquote_identifier,
)


def test_domain_schema_models_frozen() -> None:
    col = ColumnMetadata(name="id", data_type="INTEGER", nullable=False, default=None)
    with pytest.raises(AttributeError):
        col.name = "new_id"  # type: ignore[misc]

    pk = ConstraintMetadata(name="pk_users", columns=("id",), constraint_type="PRIMARY KEY")
    with pytest.raises(AttributeError):
        pk.constraint_type = "UNIQUE"  # type: ignore[misc]

    fk = ForeignKeyMetadata(
        name="fk_orders_user",
        source_table="orders",
        source_columns=("user_id",),
        target_table="users",
        target_columns=("id",),
    )
    with pytest.raises(AttributeError):
        fk.source_table = "other"  # type: ignore[misc]

    idx = IndexMetadata(name="idx_users_email", columns=("email",), unique=True)
    with pytest.raises(AttributeError):
        idx.unique = False  # type: ignore[misc]

    tbl = TableMetadata(name="users", columns=(col,), primary_key=pk)
    with pytest.raises(AttributeError):
        tbl.name = "accounts"  # type: ignore[misc]

    schema = DatabaseSchema(database_name="app", engine="sqlite", tables=(tbl,))
    with pytest.raises(AttributeError):
        schema.tables = ()  # type: ignore[misc]


def test_foreign_key_display_properties() -> None:
    fk_simple = ForeignKeyMetadata(
        name="fk_1",
        source_table="orders",
        source_columns=("user_id",),
        target_table="users",
        target_columns=("id",),
    )
    assert fk_simple.source_display == "orders.user_id"
    assert fk_simple.target_display == "users.id"

    fk_composite = ForeignKeyMetadata(
        name="fk_comp",
        source_table="order_items",
        source_columns=("order_id", "item_id"),
        target_table="items",
        target_columns=("order_id", "item_id"),
        source_schema="sales",
        target_schema="inventory",
    )
    assert fk_composite.source_display == "sales.order_items.(order_id, item_id)"
    assert fk_composite.target_display == "inventory.items.(order_id, item_id)"


def test_table_metadata_qualified_name() -> None:
    tbl_simple = TableMetadata(name="users")
    assert tbl_simple.qualified_name == "users"

    tbl_qualified = TableMetadata(name="users", schema_name="public")
    assert tbl_qualified.qualified_name == "public.users"


def test_database_schema_get_table_resolution() -> None:
    t1 = TableMetadata(name="users", schema_name="public")
    t2 = TableMetadata(name="orders", schema_name="public")
    t3 = TableMetadata(name="users", schema_name="audit")
    schema = DatabaseSchema(
        database_name="mydb",
        engine="postgresql",
        schemas=("public", "audit"),
        tables=(t1, t2, t3),
    )

    # Qualified by dot notation
    assert schema.get_table("public.users") == t1
    assert schema.get_table("audit.users") == t3

    # Qualified by schema argument
    assert schema.get_table("users", schema="public") == t1
    assert schema.get_table("users", schema="audit") == t3

    # Single match across schemas
    assert schema.get_table("orders") == t2
    assert schema.get_table("ORDERS") == t2

    # Ambiguous match raises AmbiguousObjectNameError
    with pytest.raises(AmbiguousObjectNameError) as exc_info:
        schema.get_table("users")
    assert "ambiguous across schemas" in str(exc_info.value)

    # Not found returns None
    assert schema.get_table("nonexistent") is None


def test_readonly_dict_immutability_and_json() -> None:
    d = ReadOnlyDict({"a": 1, "nested": {"b": 2, "c": [1, 2]}})
    assert d["a"] == 1
    assert isinstance(d["nested"], ReadOnlyDict)
    assert d["nested"]["b"] == 2

    # Mutation attempts must raise TypeError
    with pytest.raises(TypeError, match="ReadOnlyDict is immutable"):
        d["a"] = 2
    with pytest.raises(TypeError, match="ReadOnlyDict is immutable"):
        del d["a"]
    with pytest.raises(TypeError, match="ReadOnlyDict is immutable"):
        d.update({"new": 3})
    with pytest.raises(TypeError, match="ReadOnlyDict is immutable"):
        d.pop("a")
    with pytest.raises(TypeError, match="ReadOnlyDict is immutable"):
        d.clear()
    with pytest.raises(TypeError, match="ReadOnlyDict is immutable"):
        d["nested"]["b"] = 3

    # Must be 100% natively JSON serializable
    dumped = json.dumps(d, sort_keys=True)
    assert dumped == '{"a": 1, "nested": {"b": 2, "c": [1, 2]}}'


def test_table_metadata_deep_immutability() -> None:
    tbl = TableMetadata(
        name="events",
        options={"capped": True, "size": 1000},
        validator={"$jsonSchema": {"required": ["id"]}},
    )
    assert isinstance(tbl.options, ReadOnlyDict)
    assert isinstance(tbl.validator, ReadOnlyDict)

    with pytest.raises(TypeError, match="ReadOnlyDict is immutable"):
        tbl.options["capped"] = False
    with pytest.raises(TypeError, match="ReadOnlyDict is immutable"):
        tbl.validator["$jsonSchema"]["required"] = ("extra",)


def test_unquote_identifier() -> None:
    assert unquote_identifier('"users"') == "users"
    assert unquote_identifier("`users`") == "users"
    assert unquote_identifier("[users]") == "users"
    assert unquote_identifier("users") == "users"
    assert unquote_identifier('"audit.2024"') == "audit.2024"
    assert unquote_identifier("[audit.2024]") == "audit.2024"
    assert unquote_identifier('"partial') == '"partial'


def test_parse_object_name() -> None:
    assert parse_object_name("users") == (None, "users")
    assert parse_object_name("public.users") == ("public", "users")
    assert parse_object_name('"public"."users"') == ("public", "users")
    assert parse_object_name("[dbo].[orders]") == ("dbo", "orders")
    assert parse_object_name("`sales`.`items`") == ("sales", "items")
    # Table with dot enclosed in quotes
    assert parse_object_name('"audit.2024"') == (None, "audit.2024")
    assert parse_object_name("[audit.2024]") == (None, "audit.2024")
    # Schema with dot and table with dot
    assert parse_object_name('"my.schema"."audit.2024"') == ("my.schema", "audit.2024")


def test_database_schema_exact_case_priority_and_dotted_tables() -> None:
    t_lower = TableMetadata(name="users")
    t_upper = TableMetadata(name="USERS")
    t_dotted = TableMetadata(name="audit.2024")

    schema = DatabaseSchema(
        database_name="testdb",
        engine="postgresql",
        tables=(t_lower, t_upper, t_dotted),
    )

    # Exact case match should win
    assert schema.get_table("users") is t_lower
    assert schema.get_table("USERS") is t_upper
    assert schema.get_table('"users"') is t_lower
    assert schema.get_table('"USERS"') is t_upper

    # Dotted table name
    assert schema.get_table("audit.2024") is t_dotted
    assert schema.get_table('"audit.2024"') is t_dotted
