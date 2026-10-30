"""Comprehensive tests for controlled CRUD data modification subsystem."""

from unittest.mock import MagicMock, patch
import pytest
from sqlalchemy.exc import IntegrityError, OperationalError

from support.core.dialect import Dialect
from support.db.models import ConnectionProfile
from support.history.store import QueryHistoryStore
from support.mutation.executor import DataModificationExecutor
from support.mutation.models import ChangePlan, MutationResult, MutationType, WhereCondition
from support.mutation.planner import (
    RawMutationProposal,
    build_change_plan,
    compute_plan_hash,
    propose_mutation_plan,
)
from support.mutation.workflow import handle_mutation_workflow
from support.schema.models import ColumnSchema, DatabaseSchema, ForeignKeySchema, TableSchema
from support.workspace.state import WorkspaceSessionState


@pytest.fixture
def sample_crud_schema():
    """Schema with users, tasks, and child task_comments."""
    users_tbl = TableSchema(
        name="users",
        columns=[
            ColumnSchema(name="id", data_type="INTEGER", is_primary_key=True),
            ColumnSchema(name="username", data_type="VARCHAR(50)", nullable=False),
            ColumnSchema(name="email", data_type="VARCHAR(100)", nullable=False),
            ColumnSchema(name="full_name", data_type="VARCHAR(100)", nullable=True),
            ColumnSchema(name="is_active", data_type="BOOLEAN", nullable=False, default_value="true"),
            ColumnSchema(name="created_at", data_type="TIMESTAMP", nullable=False, default_value="CURRENT_TIMESTAMP"),
        ],
        primary_key=["id"],
    )

    tasks_tbl = TableSchema(
        name="tasks",
        columns=[
            ColumnSchema(name="id", data_type="INTEGER", is_primary_key=True),
            ColumnSchema(name="title", data_type="VARCHAR(100)", nullable=False),
            ColumnSchema(name="status", data_type="VARCHAR(20)", nullable=False, default_value="'pending'"),
            ColumnSchema(name="user_id", data_type="INTEGER", nullable=True),
        ],
        primary_key=["id"],
        foreign_keys=[
            ForeignKeySchema(
                name="fk_tasks_user",
                constrained_columns=["user_id"],
                referred_table="users",
                referred_columns=["id"],
                on_delete="CASCADE",
            )
        ],
    )

    return DatabaseSchema(
        database_name="testdb",
        dialect=Dialect.POSTGRES,
        tables={"users": users_tbl, "tasks": tasks_tbl},
    )


@pytest.fixture
def sample_profile():
    return ConnectionProfile(
        name="test_pg",
        dialect=Dialect.POSTGRES,
        host="localhost",
        port=5432,
        database="testdb",
        username="postgres",
    )


def test_valid_insert_plan_with_database_defaults(sample_crud_schema, sample_profile):
    """Verify intelligent INSERT plan populates specified columns and leaves rest to database defaults."""
    proposal = RawMutationProposal(
        operation=MutationType.INSERT,
        table_name="users",
        values={"username": "alex", "email": "alex@example.com", "full_name": "Alex Smith"},
        conditions={},
        inferred_reasons={},
    )

    plan = build_change_plan(proposal, sample_crud_schema, sample_profile)
    assert plan.is_valid is True
    assert plan.operation == MutationType.INSERT
    assert plan.table_name == "users"
    assert plan.fields_to_set == {"username": "alex", "email": "alex@example.com", "full_name": "Alex Smith"}

    # Database defaults listed for omitted fields
    assert any("id" in df and "auto-generated primary key" in df for df in plan.default_fields)
    assert any("is_active" in df and "default" in df for df in plan.default_fields)
    assert any("created_at" in df and "default" in df for df in plan.default_fields)

    # Parameterized SQL and parameters
    assert "INSERT INTO users" in plan.sql
    assert ":username" in plan.sql or "%(username)s" in plan.sql or "username" in plan.sql
    assert plan.parameters["username"] == "alex"
    assert plan.parameters["email"] == "alex@example.com"


def test_invalid_insert_plan_missing_required_fields(sample_crud_schema, sample_profile):
    """Verify INSERT plan fails validation when mandatory columns are omitted."""
    # Omit 'email' which is NOT NULL with no default
    proposal = RawMutationProposal(
        operation=MutationType.INSERT,
        table_name="users",
        values={"username": "alex"},
        conditions={},
        inferred_reasons={},
    )

    plan = build_change_plan(proposal, sample_crud_schema, sample_profile)
    assert plan.is_valid is False
    assert "email" in plan.missing_required_fields
    assert "Required field(s) missing" in plan.validation_error


def test_parameterized_statements_resist_sql_injection(sample_crud_schema, sample_profile):
    """Verify values with injection syntax are strictly bound as parameters and never concatenated."""
    injection_val = "admin'; DROP TABLE users; --"
    proposal = RawMutationProposal(
        operation=MutationType.INSERT,
        table_name="users",
        values={"username": injection_val, "email": "test@example.com"},
        conditions={},
        inferred_reasons={},
    )

    plan = build_change_plan(proposal, sample_crud_schema, sample_profile)
    assert plan.is_valid is True
    # The injection string must be in parameters, NOT in the raw SQL statement
    assert plan.parameters["username"] == injection_val
    assert "DROP TABLE" not in plan.sql


def test_update_plan_targeting_exact_records(sample_crud_schema, sample_profile):
    """Verify UPDATE plan compiles parameterized conditions and values."""
    proposal = RawMutationProposal(
        operation=MutationType.UPDATE,
        table_name="tasks",
        values={"status": "completed"},
        conditions={"id": 42},
        inferred_reasons={},
    )

    plan = build_change_plan(
        proposal,
        sample_crud_schema,
        sample_profile,
        preview_records=[{"id": 42, "title": "Deploy code", "status": "pending"}],
        matched_count=1,
    )
    assert plan.is_valid is True
    assert plan.operation == MutationType.UPDATE
    assert plan.fields_to_set == {"status": "completed"}
    assert len(plan.conditions) == 1
    assert plan.conditions[0].column == "id"
    assert plan.conditions[0].value == 42
    assert "UPDATE tasks" in plan.sql
    assert plan.matched_record_count == 1


def test_update_plan_rejects_unconditional_update(sample_crud_schema, sample_profile):
    """Verify UPDATE plan without conditions is strictly rejected."""
    proposal = RawMutationProposal(
        operation=MutationType.UPDATE,
        table_name="tasks",
        values={"status": "completed"},
        conditions={},  # Empty WHERE clause
        inferred_reasons={},
    )

    plan = build_change_plan(proposal, sample_crud_schema, sample_profile)
    assert plan.is_valid is False
    assert "Unconditional UPDATE without a WHERE clause is prohibited" in plan.validation_error


def test_delete_plan_with_foreign_key_warnings(sample_crud_schema, sample_profile):
    """Verify DELETE plan inspects foreign keys and identifies child references."""
    # Deleting from 'users' is referenced by 'tasks.user_id'
    proposal = RawMutationProposal(
        operation=MutationType.DELETE,
        table_name="users",
        values={},
        conditions={"id": 5},
        inferred_reasons={},
    )

    plan = build_change_plan(
        proposal,
        sample_crud_schema,
        sample_profile,
        preview_records=[{"id": 5, "username": "bob", "email": "bob@example.com"}],
        matched_count=1,
    )
    assert plan.is_valid is True
    assert plan.operation == MutationType.DELETE
    assert len(plan.foreign_key_warnings) > 0
    assert "tasks" in plan.foreign_key_warnings[0]


def test_delete_plan_rejects_unconditional_delete(sample_crud_schema, sample_profile):
    """Verify unconditional DELETE is strictly rejected."""
    proposal = RawMutationProposal(
        operation=MutationType.DELETE,
        table_name="tasks",
        values={},
        conditions={},
        inferred_reasons={},
    )

    plan = build_change_plan(proposal, sample_crud_schema, sample_profile)
    assert plan.is_valid is False
    assert "Unconditional DELETE without a WHERE clause is prohibited" in plan.validation_error


def test_concurrency_conflict_aborts_and_rolls_back(sample_crud_schema, sample_profile):
    """Verify execution rolls back if matched records change between preview and execution."""
    plan = build_change_plan(
        proposal=RawMutationProposal(
            operation=MutationType.UPDATE,
            table_name="tasks",
            values={"status": "completed"},
            conditions={"id": 42},
            inferred_reasons={},
        ),
        schema=sample_crud_schema,
        profile=sample_profile,
        matched_count=1,
    )

    mock_factory = MagicMock()
    mock_engine = MagicMock()
    mock_conn = MagicMock()
    mock_trans = MagicMock()

    mock_factory.create_engine.return_value = mock_engine
    mock_engine.connect.return_value.__enter__.return_value = mock_conn
    mock_conn.begin.return_value.__enter__.return_value = mock_trans

    # When checking count in transaction, record was deleted concurrently (count is 0 instead of 1)
    mock_conn.execute.return_value.scalar.return_value = 0

    executor = DataModificationExecutor(connection_factory=mock_factory)
    result = executor.execute_mutation(sample_profile, password=None, plan=plan)

    assert result.is_success is False
    assert result.status == "ROLLED_BACK"
    assert result.error_type == "CONCURRENCY_CONFLICT"
    assert result.rolled_back is True
    mock_trans.rollback.assert_called_once()
    mock_trans.commit.assert_not_called()


def test_transaction_rollback_on_unique_violation(sample_crud_schema, sample_profile):
    """Verify unique constraint violations roll back transaction with safe sanitized message."""
    plan = build_change_plan(
        proposal=RawMutationProposal(
            operation=MutationType.INSERT,
            table_name="users",
            values={"username": "alice", "email": "alice@example.com"},
            conditions={},
            inferred_reasons={},
        ),
        schema=sample_crud_schema,
        profile=sample_profile,
    )

    mock_factory = MagicMock()
    mock_engine = MagicMock()
    mock_conn = MagicMock()
    mock_trans = MagicMock()

    mock_factory.create_engine.return_value = mock_engine
    mock_engine.connect.return_value.__enter__.return_value = mock_conn
    mock_conn.begin.return_value.__enter__.return_value = mock_trans

    # Driver raises unique integrity violation
    mock_conn.execute.side_effect = IntegrityError(
        statement="INSERT...",
        params={},
        orig=Exception("duplicate key value violates unique constraint 'users_username_key'"),
    )

    executor = DataModificationExecutor(connection_factory=mock_factory)
    result = executor.execute_mutation(sample_profile, password=None, plan=plan)

    assert result.is_success is False
    assert result.status == "ROLLED_BACK"
    assert result.error_type == "UNIQUE_VIOLATION"
    assert "A record with this value already exists" in result.error_message
    assert result.rolled_back is True


def test_transaction_rollback_on_foreign_key_violation(sample_crud_schema, sample_profile):
    """Verify foreign key constraint violations roll back transaction."""
    plan = build_change_plan(
        proposal=RawMutationProposal(
            operation=MutationType.DELETE,
            table_name="users",
            values={},
            conditions={"id": 1},
            inferred_reasons={},
        ),
        schema=sample_crud_schema,
        profile=sample_profile,
        matched_count=1,
    )

    mock_factory = MagicMock()
    mock_engine = MagicMock()
    mock_conn = MagicMock()
    mock_trans = MagicMock()

    mock_factory.create_engine.return_value = mock_engine
    mock_engine.connect.return_value.__enter__.return_value = mock_conn
    mock_conn.begin.return_value.__enter__.return_value = mock_trans

    # Driver raises foreign key violation
    mock_conn.execute.side_effect = [
        MagicMock(),  # SET statement_timeout
        MagicMock(scalar=lambda: 1),  # Concurrency count check matches 1
        IntegrityError(
            statement="DELETE...",
            params={},
            orig=Exception("update or delete on table 'users' violates foreign key constraint 'fk_tasks_user'"),
        ),
    ]

    executor = DataModificationExecutor(connection_factory=mock_factory)
    result = executor.execute_mutation(sample_profile, password=None, plan=plan)

    assert result.is_success is False
    assert result.status == "ROLLED_BACK"
    assert result.error_type == "FOREIGN_KEY_VIOLATION"
    assert "Foreign key constraint violation" in result.error_message


def test_transaction_rollback_on_timeout(sample_crud_schema, sample_profile):
    """Verify timeout cancels execution and safely rolls back transaction."""
    plan = build_change_plan(
        proposal=RawMutationProposal(
            operation=MutationType.UPDATE,
            table_name="tasks",
            values={"status": "completed"},
            conditions={"id": 42},
            inferred_reasons={},
        ),
        schema=sample_crud_schema,
        profile=sample_profile,
        matched_count=1,
    )

    mock_factory = MagicMock()
    mock_engine = MagicMock()
    mock_conn = MagicMock()
    mock_trans = MagicMock()

    mock_factory.create_engine.return_value = mock_engine
    mock_engine.connect.return_value.__enter__.return_value = mock_conn
    mock_conn.begin.return_value.__enter__.return_value = mock_trans

    mock_conn.execute.side_effect = [
        MagicMock(),  # SET statement_timeout
        MagicMock(scalar=lambda: 1),  # count check
        OperationalError(
            statement="UPDATE...",
            params={},
            orig=Exception("canceling statement due to statement timeout"),
        ),
    ]

    executor = DataModificationExecutor(connection_factory=mock_factory)
    result = executor.execute_mutation(sample_profile, password=None, plan=plan)

    assert result.is_success is False
    assert result.status == "ROLLED_BACK"
    assert result.error_type == "TIMEOUT"
    assert "Execution timed out" in result.error_message


def test_mongodb_rejected_by_relational_write_pipeline():
    """Verify MongoDB write operations are rejected and never routed to relational engine."""
    mongo_prof = ConnectionProfile(name="mongo_db", dialect=Dialect.MONGODB, database="appdb")
    plan = ChangePlan(
        plan_id="p1",
        operation=MutationType.INSERT,
        database="mongo_db",
        dialect="mongodb",
        table_name="users",
        fields_to_set={"username": "alex"},
        is_valid=True,
    )

    executor = DataModificationExecutor()
    result = executor.execute_mutation(mongo_prof, password=None, plan=plan)
    assert result.is_success is False
    assert result.error_type == "UNSUPPORTED_DIALECT"
    assert "MongoDB write operations are not supported" in result.error_message


def test_workflow_explicit_confirmation_defaults_to_no(sample_crud_schema, sample_profile):
    """Verify conversational workflow cancels when user presses Enter without 'y'."""
    state = WorkspaceSessionState(active_database="test_pg", database_profile=sample_profile)
    state.cached_schema = sample_crud_schema

    mock_exec = MagicMock(spec=DataModificationExecutor)
    mock_db = MagicMock()
    mock_db.manager.get_profile_with_password.return_value = (sample_profile, None)

    # User enters "" (presses Enter, defaulting to No)
    handled = handle_mutation_workflow(
        question="Add a user named Alex with email alex@example.com",
        state=state,
        db_service=mock_db,
        executor=mock_exec,
        user_input_func=lambda p: "",
    )

    assert handled is False
    mock_exec.execute_mutation.assert_not_called()


def test_workflow_approved_execution_records_history(tmp_path, sample_crud_schema, sample_profile):
    """Verify approved write executes and writes metadata to history without storing sensitive data."""
    store = QueryHistoryStore(config_dir=tmp_path)
    state = WorkspaceSessionState(active_database="test_pg", database_profile=sample_profile)
    state.cached_schema = sample_crud_schema

    mock_exec = MagicMock(spec=DataModificationExecutor)
    mock_db = MagicMock()
    mock_db.manager.get_profile_with_password.return_value = (sample_profile, None)

    mock_exec.execute_mutation.return_value = MutationResult(
        is_success=True,
        status="COMMITTED",
        operation="INSERT",
        database="test_pg",
        table="users",
        affected_rows=1,
        duration_ms=18.0,
        inserted_id=99,
        committed=True,
    )

    handled = handle_mutation_workflow(
        question="Add a user named Alex with email alex@example.com",
        state=state,
        db_service=mock_db,
        executor=mock_exec,
        user_input_func=lambda p: "y",  # User confirms
        store=store,
    )

    assert handled is True
    mock_exec.execute_mutation.assert_called_once()

    entries = store.list_entries()
    assert len(entries) == 1
    assert entries[0].entry_type == "mutation"
    assert entries[0].database == "test_pg"
    assert entries[0].execution_status == "COMMITTED"
    assert entries[0].row_count == 1


def test_broad_delete_requires_exact_delete_confirmation(sample_crud_schema, sample_profile):
    """Verify deleting multiple records requires typing 'DELETE' rather than a simple 'y'."""
    state = WorkspaceSessionState(active_database="test_pg", database_profile=sample_profile)
    state.cached_schema = sample_crud_schema

    mock_exec = MagicMock(spec=DataModificationExecutor)
    mock_db = MagicMock()
    mock_db.manager.get_profile_with_password.return_value = (sample_profile, None)

    # 5 matching records found for deletion
    mock_exec.inspect_target_records.return_value = (
        [{"id": i, "status": "closed"} for i in range(1, 6)],
        5,
    )

    # 1. User inputs "y" -> rejected because broad delete needs exact word 'DELETE'
    handled_y = handle_mutation_workflow(
        question="Delete tasks where status = closed",
        state=state,
        db_service=mock_db,
        executor=mock_exec,
        user_input_func=lambda p: "y",
    )
    assert handled_y is False
    mock_exec.execute_mutation.assert_not_called()

    # 2. User inputs "DELETE" -> approved and executed
    mock_exec.execute_mutation.return_value = MutationResult(
        is_success=True,
        status="COMMITTED",
        operation="DELETE",
        database="test_pg",
        table="tasks",
        affected_rows=5,
        committed=True,
    )

    handled_delete = handle_mutation_workflow(
        question="Delete tasks where status = closed",
        state=state,
        db_service=mock_db,
        executor=mock_exec,
        user_input_func=lambda p: "DELETE",
    )
    assert handled_delete is True
    mock_exec.execute_mutation.assert_called_once()


def test_plan_hash_tampering_aborts_execution(sample_crud_schema, sample_profile):
    """Verify execution aborts if change plan was modified after approval."""
    state = WorkspaceSessionState(active_database="test_pg", database_profile=sample_profile)
    state.cached_schema = sample_crud_schema

    mock_exec = MagicMock(spec=DataModificationExecutor)
    mock_db = MagicMock()
    mock_db.manager.get_profile_with_password.return_value = (sample_profile, None)

    proposal = RawMutationProposal(
        operation=MutationType.INSERT,
        table_name="users",
        values={"username": "alex", "email": "alex@example.com"},
        conditions={},
        inferred_reasons={},
    )
    plan = build_change_plan(proposal, sample_crud_schema, sample_profile)

    # Tamper with the plan SQL after hash computation
    plan.sql = "INSERT INTO users (username) VALUES ('hacked'); DROP TABLE users;"

    with patch("support.mutation.workflow.propose_mutation_plan", return_value=proposal), \
         patch("support.mutation.workflow.build_change_plan", return_value=plan):

        handled = handle_mutation_workflow(
            question="Add a user named Alex",
            state=state,
            db_service=mock_db,
            executor=mock_exec,
            user_input_func=lambda p: "y",
        )

        assert handled is False
        mock_exec.execute_mutation.assert_not_called()


def test_missing_required_fields_follow_up_prompting(sample_crud_schema, sample_profile):
    """Verify missing required fields prompt the user interactively before plan compilation."""
    state = WorkspaceSessionState(active_database="test_pg", database_profile=sample_profile)
    state.cached_schema = sample_crud_schema

    mock_exec = MagicMock(spec=DataModificationExecutor)
    mock_db = MagicMock()
    mock_db.manager.get_profile_with_password.return_value = (sample_profile, None)

    # User supplies values during follow-up prompts
    prompts_and_answers = [
        "john_doe",           # Answer for username
        "john@example.com",   # Answer for email
        "y",                  # Approval for execution
    ]

    mock_exec.execute_mutation.return_value = MutationResult(
        is_success=True,
        status="COMMITTED",
        operation="INSERT",
        database="test_pg",
        table="users",
        affected_rows=1,
        committed=True,
    )

    handled = handle_mutation_workflow(
        question="Add a new user with a username and email.",
        state=state,
        db_service=mock_db,
        executor=mock_exec,
        user_input_func=lambda p: prompts_and_answers.pop(0),
    )

    assert handled is True
    mock_exec.execute_mutation.assert_called_once()
    called_plan = mock_exec.execute_mutation.call_args[1]["plan"]
    assert called_plan.fields_to_set["username"] == "john_doe"
    assert called_plan.fields_to_set["email"] == "john@example.com"


def test_dialect_statement_construction_all_relational_dialects(sample_crud_schema):
    """Verify INSERT, UPDATE, and DELETE plans compile correctly for SQLite, MySQL, and SQL Server."""
    dialects = [
        (Dialect.SQLITE, "sqlite_db"),
        (Dialect.MYSQL, "mysql_db"),
        (Dialect.MSSQL, "mssql_db"),
    ]

    for dial, name in dialects:
        prof = ConnectionProfile(name=name, dialect=dial, database="db")
        prop = RawMutationProposal(
            operation=MutationType.INSERT,
            table_name="users",
            values={"username": "alex", "email": "alex@example.com"},
            conditions={},
            inferred_reasons={},
        )
        plan = build_change_plan(prop, sample_crud_schema, prof)
        assert plan.is_valid is True
        assert plan.dialect == dial.value
        assert "INSERT INTO users" in plan.sql
        assert plan.parameters["username"] == "alex"
        assert plan.parameters["email"] == "alex@example.com"

