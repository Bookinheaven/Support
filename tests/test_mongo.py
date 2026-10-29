"""Unit tests for MongoDB support: dialect, URL parsing, health checks, and schema inspection."""

from unittest.mock import MagicMock, patch
import pytest
from typer.testing import CliRunner

from support.cli.main import app
from support.core.dialect import Dialect
from support.core.exceptions import DatabaseAccessError
from support.db.health import check_mongodb_health, verify_driver_available
from support.db.models import ConnectionProfile
from support.db.url_parser import parse_database_url
from support.schema.sources.mongo_db import MongoDatabaseSchemaSource, _get_bson_type_name
from support.schema.mongo_models import (
    MongoDatabaseSchema,
    MongoCollectionSchema,
    MongoFieldSchema,
    MongoIndexSchema,
)

runner = CliRunner()


def test_mongodb_dialect_properties():
    """Verify MongoDB dialect properties, document classification, and string parsing."""
    assert Dialect.MONGODB.is_document_database is True
    assert Dialect.MONGODB.is_relational is False
    assert Dialect.MONGODB.display_name == "MongoDB"
    assert Dialect.MONGODB.sqlglot_read_dialect is None

    assert Dialect.from_string("mongodb") == Dialect.MONGODB
    assert Dialect.from_string("mongo") == Dialect.MONGODB
    assert Dialect.from_string("MONGODB") == Dialect.MONGODB


def test_mongodb_url_parsing():
    """Verify parsing of mongodb:// and mongodb+srv:// connection URLs."""
    url = "mongodb://admin:secret123@cluster0.example.com:27017/analytics_db?authSource=admin"
    profile, password = parse_database_url(url, name_override="my_mongo")

    assert profile.name == "my_mongo"
    assert password == "secret123"
    assert profile.dialect == Dialect.MONGODB
    assert profile.host == "cluster0.example.com"
    assert profile.port == 27017
    assert profile.database == "analytics_db"
    assert profile.username == "admin"
    assert profile.get_redacted_url() == "mongodb://admin:***@cluster0.example.com:27017/analytics_db"

    # SRV URL
    srv_url = "mongodb+srv://user:pass@cluster.mongodb.net/testdb"
    srv_profile, srv_pass = parse_database_url(srv_url)
    assert srv_profile.dialect == Dialect.MONGODB
    assert srv_pass == "pass"
    assert srv_profile.name == "testdb"
    assert srv_profile.get_redacted_url() == "mongodb+srv://user:***@cluster.mongodb.net/testdb"



def test_mongodb_driver_verification():
    """Verify driver verification detects pymongo without errors."""
    # PyMongo is installed in the test environment
    verify_driver_available(Dialect.MONGODB)


def test_mongodb_health_check_online():
    """Verify check_mongodb_health returns online with version and latency when ping succeeds."""
    profile = ConnectionProfile(
        name="mongo_local",
        dialect=Dialect.MONGODB,
        host="localhost",
        port=27017,
        database="test",
    )

    mock_client = MagicMock()
    mock_client.admin.command.side_effect = lambda cmd: (
        {"ok": 1} if cmd == "ping" else {"version": "7.0.5"} if cmd in ("buildInfo", "buildinfo") else {}
    )

    with patch("pymongo.MongoClient", return_value=mock_client):
        result = check_mongodb_health(profile, timeout=2.0)

    assert result.is_online is True
    assert "7.0.5" in str(result.server_version)
    assert result.latency_ms is not None
    assert mock_client.close.called


def test_mongodb_health_check_auth_failure():
    """Verify check_mongodb_health classifies auth failures without leaking secrets."""
    profile = ConnectionProfile(
        name="mongo_auth",
        dialect=Dialect.MONGODB,
        host="localhost",
        port=27017,
        database="test",
        username="bad_user",
    )

    from pymongo.errors import OperationFailure

    mock_client = MagicMock()
    mock_client.admin.command.side_effect = OperationFailure("Authentication failed", code=18)

    with patch("pymongo.MongoClient", return_value=mock_client):
        result = check_mongodb_health(profile, password="wrong_password", timeout=2.0)

    assert result.is_online is False
    assert result.status == "AUTH FAILED"
    assert "wrong_password" not in str(result.error_message)



def test_mongodb_schema_inspection_sampling():
    """Verify MongoDatabaseSchemaSource samples documents and reports heterogeneous fields."""
    profile = ConnectionProfile(
        name="mongo_sample",
        dialect=Dialect.MONGODB,
        host="localhost",
        port=27017,
        database="shop",
    )

    mock_client = MagicMock()
    mock_client.admin.command.side_effect = lambda cmd: (
        {"ok": 1} if cmd == "ping" else {"version": "6.0.1"} if cmd == "buildInfo" else {}
    )
    mock_client.list_database_names.return_value = ["shop", "admin", "local"]

    mock_db = MagicMock()
    mock_client.__getitem__.return_value = mock_db
    mock_db.list_collection_names.return_value = ["orders", "empty_col"]

    # Collection 1: orders (heterogeneous document structure)
    orders_col = MagicMock()
    orders_col.estimated_document_count.return_value = 2
    orders_col.list_indexes.return_value = [
        {"name": "_id_", "key": {"_id": 1}, "unique": True},
        {"name": "order_idx", "key": {"customer.id": 1}, "unique": False},
    ]
    orders_col.find.return_value.limit.return_value = [
        {
            "_id": "order_001",
            "amount": 49.99,
            "status": "shipped",
            "tags": ["retail", "promo"],
            "customer": {"id": 101, "email": "alice@example.com"},
        },
        {
            "_id": "order_002",
            "amount": 100,  # int instead of float -> polymorphic!
            "status": "pending",
            "notes": "urgent",  # optional field
            "customer": {"id": 102},
        },
    ]

    # Collection 2: empty_col
    empty_col = MagicMock()
    empty_col.estimated_document_count.return_value = 0
    empty_col.list_indexes.return_value = [{"name": "_id_", "key": {"_id": 1}, "unique": True}]

    def get_col(col_name):
        return orders_col if col_name == "orders" else empty_col

    mock_db.__getitem__.side_effect = get_col

    with patch("pymongo.MongoClient", return_value=mock_client):
        source = MongoDatabaseSchemaSource(profile, sample_limit=50, timeout=3.0)
        schema = source.discover()

    assert schema.database_name == "shop"
    assert schema.server_version == "6.0.1"
    assert "orders" in schema.collections
    assert "empty_col" in schema.collections

    # Check empty collection
    col_empty = schema.collections["empty_col"]
    assert col_empty.is_empty is True
    assert col_empty.sample_size == 0
    assert len(col_empty.fields) == 0

    # Check orders collection
    col_orders = schema.collections["orders"]
    assert col_orders.is_empty is False
    assert col_orders.sample_size == 2

    # Verify polymorphic field 'amount' observed both double and int
    amount_field = next(f for f in col_orders.fields if f.name == "amount")
    assert "double" in amount_field.types
    assert "int" in amount_field.types
    assert amount_field.count == 2
    assert amount_field.nullable is False

    # Verify optional field 'notes'
    notes_field = next(f for f in col_orders.fields if f.name == "notes")
    assert notes_field.count == 1
    assert notes_field.nullable is True
    assert notes_field.occurrence_rate == 50.0

    # Verify nested customer fields
    cust_email = next(f for f in col_orders.fields if f.name == "customer.email")
    assert cust_email.count == 1
    assert cust_email.nullable is True

    # Verify indexes
    assert len(col_orders.indexes) == 2
    assert col_orders.indexes[0].name == "_id_"
    assert col_orders.indexes[0].unique is True


def test_mongodb_cli_inspect():
    """Verify 'support db inspect' runs MongoDB schema inspection and formats results."""
    from support.db.service import DatabaseService

    profile = ConnectionProfile(
        name="mongo_inspect_test",
        dialect=Dialect.MONGODB,
        host="localhost",
        port=27017,
        database="test_db",
    )

    mock_schema = MongoDatabaseSchema(
        database_name="test_db",
        server_version="7.0.0",
        accessible_databases=["test_db"],
        sample_limit=50,
        collections={
            "users": MongoCollectionSchema(
                name="users",
                estimated_count=10,
                sample_size=10,
                fields=[
                    MongoFieldSchema(name="_id", types=["objectId"], count=10, sample_size=10, nullable=False),
                    MongoFieldSchema(name="name", types=["string"], count=10, sample_size=10, nullable=False),
                ],
                indexes=[MongoIndexSchema(name="_id_", keys=[("_id", 1)], unique=True)],
            )
        },
    )

    with patch("support.cli.db_cmds.DatabaseService") as mock_service_cls:
        mock_service = mock_service_cls.return_value
        mock_service.manager.get_profile_with_password.return_value = (profile, None)
        with patch("support.schema.sources.mongo_db.MongoDatabaseSchemaSource.discover", return_value=mock_schema):
            res = runner.invoke(app, ["db", "inspect", "mongo_inspect_test"])

    assert res.exit_code == 0
    assert "MongoDB Document Schema Discovery" in res.stdout
    assert "users" in res.stdout
    assert "objectId" in res.stdout

