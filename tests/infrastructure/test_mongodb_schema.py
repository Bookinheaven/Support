"""Unit tests for MongoDB schema introspection."""

from unittest.mock import MagicMock, patch
import pytest

pytest.importorskip("pymongo")

from support.application.errors import ObjectNotFoundError
from support.domain.profiles import MongoDBConnection
from support.domain.schema import TableType
from support.infrastructure.mongodb import MongoDBConnector


def test_mongodb_schema_introspection_no_find_queries() -> None:
    connector = MongoDBConnector()
    conn_config = MongoDBConnection(uri="mongodb://localhost:27017", database="analytics")

    mock_client = MagicMock()
    mock_db = MagicMock()
    mock_client.__getitem__.return_value = mock_db

    # Mock list_collections returning 2 collections with validator & options
    col1_info = {
        "name": "events",
        "type": "collection",
        "options": {
            "capped": True,
            "size": 1048576,
            "validator": {"$jsonSchema": {"bsonType": "object", "required": ["event_type"]}},
        },
    }
    col2_info = {
        "name": "users",
        "type": "collection",
        "options": {},
    }
    mock_db.list_collections.return_value = [col1_info, col2_info]

    # Mock index_information
    mock_col_events = MagicMock()
    mock_col_events.index_information.return_value = {
        "_id_": {"key": [("_id", 1)], "v": 2},
        "loc_2dsphere": {"key": [("location", "2dsphere")], "v": 2},
        "ts_desc": {
            "key": [("ts", -1)],
            "partialFilterExpression": {"rating": {"$gt": 5}},
            "v": 2,
        },
    }
    mock_col_users = MagicMock()
    mock_col_users.index_information.return_value = {
        "_id_": {"key": [("_id", 1)], "v": 2},
        "email_unique": {"key": [("email", 1)], "unique": True, "v": 2},
    }

    def get_collection(name: str):
        if name == "events":
            return mock_col_events
        return mock_col_users

    mock_db.__getitem__.side_effect = get_collection

    with patch("pymongo.MongoClient", return_value=mock_client):
        schema = connector.introspect_schema(conn_config)

    assert schema.database_name == "analytics"
    assert schema.engine == "MongoDB"
    assert len(schema.tables) == 2

    events = schema.get_table("events")
    assert events is not None
    assert events.table_type == TableType.COLLECTION
    assert events.options.get("capped") is True
    assert events.options.get("size") == 1048576
    assert events.validator == {"$jsonSchema": {"bsonType": "object", "required": ("event_type",)}}
    assert len(events.indexes) == 3

    loc_idx = next(i for i in events.indexes if i.name == "loc_2dsphere")
    assert loc_idx.method == "2dsphere"
    assert loc_idx.column_orders == ("2dsphere",)

    ts_idx = next(i for i in events.indexes if i.name == "ts_desc")
    assert ts_idx.column_orders == ("DESC",)
    assert ts_idx.filter_condition is not None
    assert "rating" in ts_idx.filter_condition

    users = schema.get_table("users")
    assert users is not None
    assert any(idx.unique for idx in users.indexes)

    # CRITICAL: Verify NO find() or document inspection was called
    mock_col_events.find.assert_not_called()
    mock_col_events.find_one.assert_not_called()
    mock_col_users.find.assert_not_called()
    mock_col_users.find_one.assert_not_called()
    mock_db.command.assert_not_called()


def test_mongodb_introspect_table_not_found() -> None:
    connector = MongoDBConnector()
    conn_config = MongoDBConnection(uri="mongodb://localhost:27017", database="analytics")

    mock_client = MagicMock()
    mock_db = MagicMock()
    mock_client.__getitem__.return_value = mock_db
    mock_db.list_collection_names.return_value = ["events"]

    with patch("pymongo.MongoClient", return_value=mock_client):
        with pytest.raises(ObjectNotFoundError):
            connector.introspect_table(conn_config, "nonexistent")
