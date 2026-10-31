"""Automated tests for ambiguous request clarification and state management."""

from unittest.mock import MagicMock, patch

import pytest

from support.core.dialect import Dialect
from support.db.models import ConnectionProfile
from support.db.service import DatabaseService
from support.llm.models import GenerationResponse
from support.llm.service import LLMService
from support.query.generator import QueryGenerator
from support.query.models import QueryDraft
from support.schema.models import ColumnSchema, DatabaseSchema, ForeignKeySchema, TableSchema
from support.workspace.execution_commands import (
    handle_conversational_execution,
    handle_run_command,
)
from support.workspace.session import run_workspace_session
from support.workspace.state import PendingClarification, WorkspaceSessionState


@pytest.fixture
def mock_ecommerce_schema():
    """Build a rich relational schema with users, orders, and products."""
    schema = DatabaseSchema(dialect=Dialect.POSTGRES, database_name="devdb")
    users = TableSchema(
        name="users",
        primary_key=["id"],
        columns=[
            ColumnSchema(name="id", data_type="INTEGER", is_primary_key=True),
            ColumnSchema(name="username", data_type="VARCHAR"),
            ColumnSchema(name="full_name", data_type="VARCHAR"),
            ColumnSchema(name="email", data_type="VARCHAR"),
            ColumnSchema(name="created_at", data_type="TIMESTAMP"),
        ],
    )
    orders = TableSchema(
        name="orders",
        primary_key=["id"],
        columns=[
            ColumnSchema(name="id", data_type="INTEGER", is_primary_key=True),
            ColumnSchema(name="user_id", data_type="INTEGER"),
            ColumnSchema(name="total_amount", data_type="NUMERIC"),
            ColumnSchema(name="status", data_type="VARCHAR"),
            ColumnSchema(name="created_at", data_type="TIMESTAMP"),
        ],
        foreign_keys=[
            ForeignKeySchema(
                constrained_columns=["user_id"],
                referred_table="users",
                referred_columns=["id"],
            )
        ],
    )
    products = TableSchema(
        name="products",
        primary_key=["id"],
        columns=[
            ColumnSchema(name="id", data_type="INTEGER", is_primary_key=True),
            ColumnSchema(name="name", data_type="VARCHAR"),
            ColumnSchema(name="price", data_type="NUMERIC"),
            ColumnSchema(name="stock", data_type="INTEGER"),
        ],
    )
    schema.tables["users"] = users
    schema.tables["orders"] = orders
    schema.tables["products"] = products
    return schema


@pytest.fixture
def mock_session_state(mock_ecommerce_schema):
    """Create initialized WorkspaceSessionState with cached schema."""
    profile = ConnectionProfile(name="dev-pg", dialect=Dialect.POSTGRES, database="devdb")
    state = WorkspaceSessionState(
        active_database="dev-pg",
        database_profile=profile,
        database_status="ONLINE",
    )
    state.cached_schema = mock_ecommerce_schema
    return state


# 1. Clear requests generate SQL without clarification
def test_clear_request_generates_sql_without_clarification(mock_session_state):
    """Verify concrete, actionable requests generate SQL directly without asking clarification."""
    mock_llm = MagicMock(spec=LLMService)
    mock_llm.generate.return_value = GenerationResponse(
        text="```sql\nSELECT id, username, email FROM users ORDER BY created_at LIMIT 10;\n```\nExplanation: Shows the first 10 users.",
        provider="ollama",
        model="qwen2.5-coder:7b",
    )
    generator = QueryGenerator(llm_service=mock_llm)
    draft = generator.generate_draft("Show the first 10 users.", mock_session_state)

    assert draft.is_clarification is False
    assert draft.is_validated is True
    assert "LIMIT 10" in draft.sql
    assert draft.clarification is None


# 2. "Find the best customers" asks for a ranking criterion
def test_find_best_customers_asks_for_ranking_criterion(mock_session_state):
    """Verify 'Find the best customers' produces a clarification question without executable SQL."""
    mock_llm = MagicMock(spec=LLMService)
    mock_llm.generate.return_value = GenerationResponse(
        text="Clarification: Should I rank customers by total purchase value, number of orders, or something else?",
        provider="ollama",
        model="qwen2.5-coder:7b",
    )
    generator = QueryGenerator(llm_service=mock_llm)
    draft = generator.generate_draft("Find the best customers.", mock_session_state)

    assert draft.is_clarification is True
    assert draft.sql == ""
    assert draft.is_validated is False
    assert "total purchase value" in draft.clarification
    assert draft.validation_status == "Clarification needed"


# 3. "Show important records" asks for clarification
def test_show_important_records_asks_for_clarification(mock_session_state):
    """Verify 'Show me the important records' asks for target records and importance criterion."""
    mock_llm = MagicMock(spec=LLMService)
    mock_llm.generate.return_value = GenerationResponse(
        text="Clarification: Which records are you interested in, and what should determine their importance?",
        provider="ollama",
        model="qwen2.5-coder:7b",
    )
    generator = QueryGenerator(llm_service=mock_llm)
    draft = generator.generate_draft("Show me the important records.", mock_session_state)

    assert draft.is_clarification is True
    assert draft.sql == ""
    assert "importance" in draft.clarification.lower()


# 4. "Show top users" asks for a metric when none is established
def test_show_top_users_asks_for_metric_when_none_established(mock_session_state):
    """Verify 'Show the top users' requests a ranking metric when none was established in history."""
    mock_llm = MagicMock(spec=LLMService)
    mock_llm.generate.return_value = GenerationResponse(
        text="Clarification: What metric should I rank users by: number of orders, total spending, or another metric?",
        provider="ollama",
        model="qwen2.5-coder:7b",
    )
    generator = QueryGenerator(llm_service=mock_llm)
    draft = generator.generate_draft("Show the top users.", mock_session_state)

    assert draft.is_clarification is True
    assert draft.sql == ""
    assert "metric" in draft.clarification.lower()


# 5. Clarification responses resume the original request
def test_clarification_responses_resume_original_request(mock_session_state):
    """Verify user clarification response resumes original request and synthesizes complete SQL."""
    # Turn 1 established pending clarification
    mock_session_state.pending_clarification = PendingClarification(
        original_question="Find the best customers.",
        clarification_question="What metric should I use: total purchase value or number of orders?",
        database_name="dev-pg",
    )

    mock_llm = MagicMock(spec=LLMService)
    mock_llm.generate.return_value = GenerationResponse(
        text=(
            "```sql\n"
            "SELECT u.id, u.full_name, u.email, SUM(o.total_amount) AS total_spend\n"
            "FROM users u\n"
            "JOIN orders o ON u.id = o.user_id\n"
            "GROUP BY u.id, u.full_name, u.email\n"
            "ORDER BY total_spend DESC;\n"
            "```\n"
            "Explanation: Finds best customers ranked by total purchase value."
        ),
        provider="ollama",
        model="qwen2.5-coder:7b",
    )
    generator = QueryGenerator(llm_service=mock_llm)
    draft = generator.generate_draft("Total purchase value.", mock_session_state)

    assert draft.is_clarification is False
    assert draft.is_validated is True
    assert "SUM(o.total_amount)" in draft.sql
    assert "Find the best customers." in draft.question
    assert "Total purchase value." in draft.question


# 6. Incomplete answers trigger another focused clarification
def test_incomplete_answer_triggers_another_focused_clarification(mock_session_state):
    """Verify incomplete user clarification answer asks another focused clarification question."""
    mock_session_state.pending_clarification = PendingClarification(
        original_question="Show me the important records.",
        clarification_question="Which records are you interested in, and what determines their importance?",
        database_name="dev-pg",
    )

    mock_llm = MagicMock(spec=LLMService)
    mock_llm.generate.return_value = GenerationResponse(
        text="Clarification: What criteria should determine the importance of orders: total amount, recency, or status?",
        provider="ollama",
        model="qwen2.5-coder:7b",
    )
    generator = QueryGenerator(llm_service=mock_llm)
    draft = generator.generate_draft("Orders.", mock_session_state)

    assert draft.is_clarification is True
    assert draft.sql == ""
    assert "criteria" in draft.clarification.lower() or "orders" in draft.clarification.lower()


# 7. A topic switch does not inherit unrelated pending clarification
def test_topic_switch_does_not_inherit_unrelated_pending_clarification(mock_session_state):
    """Verify switching topics discards pending clarification and fulfills new query directly."""
    mock_session_state.pending_clarification = PendingClarification(
        original_question="Find the best customers.",
        clarification_question="What metric defines best: total purchase value or order count?",
        database_name="dev-pg",
    )

    mock_llm = MagicMock(spec=LLMService)
    mock_llm.generate.return_value = GenerationResponse(
        text="```sql\nSELECT id, name, price, stock FROM products;\n```\nExplanation: Lists all products.",
        provider="ollama",
        model="qwen2.5-coder:7b",
    )
    generator = QueryGenerator(llm_service=mock_llm)
    draft = generator.generate_draft("Show all products.", mock_session_state)

    assert draft.is_clarification is False
    assert "products" in draft.sql.lower()
    assert "customers" not in draft.sql.lower()
    assert "orders" not in draft.sql.lower()


# 8. Canceling clarification discards the pending request
def test_canceling_clarification_discards_pending_request(mock_session_state):
    """Verify entering cancel/abort clears pending clarification without calling LLM or generating SQL."""
    mock_session_state.pending_clarification = PendingClarification(
        original_question="Find the best customers.",
        clarification_question="Should I rank by total spend?",
        database_name="dev-pg",
    )

    # Simulate user sending 'cancel' in workspace session loop
    responses = ["cancel", "/exit"]
    input_idx = 0

    def mock_input(prompt):
        nonlocal input_idx
        res = responses[input_idx]
        input_idx += 1
        return res

    mock_db = MagicMock(spec=DatabaseService)
    mock_llm = MagicMock(spec=LLMService)

    with patch("support.workspace.session.WorkspaceSessionState.initialize", return_value=mock_session_state):
        with patch("support.workspace.session.WorkspaceStatusChecker"):
            run_workspace_session(db_service=mock_db, llm_service=mock_llm, input_func=mock_input)

    assert mock_session_state.pending_clarification is None
    # No SQL was executed or drafted
    mock_llm.generate.assert_not_called()


# 9. Destructive ambiguity cannot execute
def test_destructive_ambiguity_cannot_execute(mock_session_state):
    """Verify 'Delete the old records' asks for target and threshold, and produces no executable SQL."""
    mock_llm = MagicMock(spec=LLMService)
    mock_llm.generate.return_value = GenerationResponse(
        text="Clarification: Which records should be deleted, and what age threshold should I use?",
        provider="ollama",
        model="qwen2.5-coder:7b",
    )
    generator = QueryGenerator(llm_service=mock_llm)
    draft = generator.generate_draft("Delete the old records.", mock_session_state)

    assert draft.is_clarification is True
    assert draft.sql == ""
    assert draft.is_validated is False

    # Verify execution handler rejects draft immediately
    mock_exec = MagicMock()
    can_exec = handle_conversational_execution(draft, mock_session_state, executor=mock_exec)
    assert can_exec is False
    mock_exec.execute.assert_not_called()


# 10. A previously established conversational definition is reused
def test_previously_established_conversational_definition_is_reused(mock_session_state):
    """Verify previously clarified metric from conversation history is reused without reprompting."""
    # Prior turn in history established 'best customers' means total purchase spend
    mock_session_state.add_message("user", "Find the best customers.")
    mock_session_state.add_message(
        "assistant",
        "Generated query draft",
        metadata={
            "sql": "SELECT u.id, SUM(o.total_amount) AS total_spend FROM users u JOIN orders o ON u.id = o.user_id GROUP BY u.id ORDER BY total_spend DESC;",
            "draft_sql": "SELECT u.id, SUM(o.total_amount) AS total_spend FROM users u JOIN orders o ON u.id = o.user_id GROUP BY u.id ORDER BY total_spend DESC;",
        },
    )

    mock_llm = MagicMock(spec=LLMService)
    mock_llm.generate.return_value = GenerationResponse(
        text=(
            "```sql\n"
            "SELECT u.id, u.full_name, SUM(o.total_amount) AS total_spend\n"
            "FROM users u\n"
            "JOIN orders o ON u.id = o.user_id\n"
            "GROUP BY u.id, u.full_name\n"
            "ORDER BY total_spend DESC\n"
            "LIMIT 5;\n"
            "```\n"
            "Explanation: Shows the top 5 customers using the previously established total spend metric."
        ),
        provider="ollama",
        model="qwen2.5-coder:7b",
    )
    generator = QueryGenerator(llm_service=mock_llm)
    draft = generator.generate_draft("Show the top five.", mock_session_state)

    assert draft.is_clarification is False
    assert draft.is_validated is True
    assert "LIMIT 5" in draft.sql
    assert "total_spend" in draft.sql


# 11. Clarification output never reaches SQL validation or execution
def test_clarification_output_never_reaches_sql_validation_or_execution(mock_session_state):
    """Verify draft with is_clarification=True is safely blocked from handle_run_command and handle_conversational_execution."""
    clarification_draft = QueryDraft(
        question="Find the best customers.",
        sql="",
        explanation="Should I rank by total purchase value or number of orders?",
        dialect="postgres",
        validation_status="Clarification needed",
        is_validated=False,
        is_parsed=False,
        clarification="Should I rank by total purchase value or number of orders?",
    )
    mock_session_state.last_query_draft = clarification_draft

    mock_input = MagicMock(return_value="y")
    mock_exec = MagicMock()

    # handle_conversational_execution must return False without calling executor
    res_conv = handle_conversational_execution(
        draft=clarification_draft,
        state=mock_session_state,
        user_input_func=mock_input,
        executor=mock_exec,
    )
    assert res_conv is False
    mock_input.assert_not_called()
    mock_exec.execute.assert_not_called()

    # handle_run_command must return False without calling executor
    res_run = handle_run_command(
        state=mock_session_state,
        user_input_func=mock_input,
        executor=mock_exec,
    )
    assert res_run is False
    mock_input.assert_not_called()
    mock_exec.execute.assert_not_called()
