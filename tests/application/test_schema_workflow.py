"""Unit tests for SchemaIntrospectionWorkflow."""

from unittest.mock import MagicMock
from uuid import uuid4
import pytest

from support.application.errors import ObjectNotFoundError, ProfileNotFoundError
from support.application.schema import SchemaIntrospectionWorkflow
from support.domain.profiles import DatabaseProfile, DatabaseType, PostgresConnection, SQLiteConnection
from support.domain.schema import DatabaseSchema, TableMetadata


def test_schema_workflow_profile_not_found() -> None:
    repo = MagicMock()
    repo.get_by_name.return_value = None
    introspector = MagicMock()
    workflow = SchemaIntrospectionWorkflow(repository=repo, introspector=introspector)

    with pytest.raises(ProfileNotFoundError):
        workflow.get_schema("unknown")


def test_schema_workflow_sqlite_introspection() -> None:
    repo = MagicMock()
    profile = DatabaseProfile(
        id=uuid4(),
        name="my-sqlite",
        connection=SQLiteConnection(database_path="/tmp/test.db"),
    )
    repo.get_by_name.return_value = profile

    expected_schema = DatabaseSchema(
        database_name="test.db",
        engine="sqlite",
        tables=(TableMetadata(name="users"),),
    )
    introspector = MagicMock()
    introspector.introspect_schema.return_value = expected_schema

    workflow = SchemaIntrospectionWorkflow(repository=repo, introspector=introspector)
    schema = workflow.get_schema("my-sqlite")

    assert schema == expected_schema
    introspector.introspect_schema.assert_called_once_with(profile.connection, password=None)


def test_schema_workflow_resolves_password_for_remote_db() -> None:
    repo = MagicMock()
    profile_id = uuid4()
    profile = DatabaseProfile(
        id=profile_id,
        name="my-pg",
        connection=PostgresConnection(database="prod", username="admin", ssl_mode="prefer"),
    )
    repo.get_by_name.return_value = profile

    cred_store = MagicMock()
    cred_store.get_password.return_value = "supersecret"

    introspector = MagicMock()
    expected_schema = DatabaseSchema(database_name="prod", engine="postgresql")
    introspector.introspect_schema.return_value = expected_schema

    workflow = SchemaIntrospectionWorkflow(
        repository=repo,
        introspector=introspector,
        credential_store=cred_store,
    )
    schema = workflow.get_schema("my-pg")

    assert schema == expected_schema
    cred_store.get_password.assert_called_once_with(profile_id)
    introspector.introspect_schema.assert_called_once_with(profile.connection, password="supersecret")


def test_schema_workflow_describe_object() -> None:
    repo = MagicMock()
    profile = DatabaseProfile(
        id=uuid4(),
        name="my-pg",
        connection=PostgresConnection(database="prod", username="admin", ssl_mode="prefer"),
    )
    repo.get_by_name.return_value = profile

    introspector = MagicMock()
    expected_table = TableMetadata(name="users", schema_name="public")
    introspector.introspect_table.return_value = expected_table

    workflow = SchemaIntrospectionWorkflow(repository=repo, introspector=introspector)

    # Simple name
    res1 = workflow.describe_object("my-pg", "users")
    assert res1 == expected_table
    introspector.introspect_table.assert_called_with(
        profile.connection,
        "users",
        schema=None,
        password=None,
    )

    # Schema dot notation
    res2 = workflow.describe_object("my-pg", "public.users")
    assert res2 == expected_table
    introspector.introspect_table.assert_called_with(
        profile.connection,
        "users",
        schema="public",
        password=None,
    )
