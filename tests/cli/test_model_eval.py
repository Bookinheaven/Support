"""Automated tests for the 12 core assistant behavioral evaluation cases (mocked deterministic tests)."""

from pathlib import Path
import sqlite3
from unittest.mock import MagicMock
import pytest

from support.application.service import ProfileService
from support.application.tools import ToolRegistry
from support.cli.composition import (
    get_assistant_workflow,
    get_connection_workflow,
    get_schema_workflow,
)
from support.domain.assistant import ChatMessage, ToolCall
from support.domain.profiles import SQLiteConnection
from support.infrastructure.keyring_credentials import KeyringCredentialStore
from support.infrastructure.sqlite_profiles import SQLiteProfileRepository


@pytest.fixture
def eval_env(tmp_path: Path):
    """Sets up an evaluation database with users and orders tables and foreign key."""
    db_path = tmp_path / "eval.db"
    conn = sqlite3.connect(str(db_path))
    with conn:
        conn.execute("CREATE TABLE users (id INTEGER PRIMARY KEY, name TEXT);")
        conn.execute(
            """
            CREATE TABLE orders (
                id INTEGER PRIMARY KEY,
                user_id INTEGER,
                FOREIGN KEY (user_id) REFERENCES users(id)
            );
            """
        )
    conn.close()

    prof_db = tmp_path / "profiles.sqlite3"
    repo = SQLiteProfileRepository(db_path=prof_db)
    creds = KeyringCredentialStore()
    service = ProfileService(repository=repo, credential_store=creds)
    service.add_profile("eval-db", SQLiteConnection(database_path=str(db_path)))

    schema_wf = get_schema_workflow(repository=repo, credential_store=creds)
    conn_wf = get_connection_workflow(repository=repo, credential_store=creds)
    tool_reg = ToolRegistry(schema_workflow=schema_wf, connection_workflow=conn_wf)

    return {"profile": "eval-db", "tool_registry": tool_reg}


def test_eval_case_1_conversational_math(eval_env) -> None:
    """Case 1: 'What is 1 + 1?' -> normal assistant text, zero tool calls."""
    mock_llm = MagicMock()
    mock_llm.chat.return_value = ChatMessage(role="assistant", content="1 + 1 is 2.")

    wf = get_assistant_workflow(llm_provider=mock_llm, tool_registry=eval_env["tool_registry"])
    res = wf.handle_user_message("What is 1 + 1?", active_profile=eval_env["profile"])

    assert res.is_error is False
    assert res.response_mode == "conversational"
    assert len(res.tool_results) == 0
    assert "2" in res.response_text


def test_eval_case_2_conversational_algebra(eval_env) -> None:
    """Case 2: 'What is (a+b)^2?' -> normal assistant text, zero tool calls."""
    mock_llm = MagicMock()
    mock_llm.chat.return_value = ChatMessage(role="assistant", content="(a+b)^2 = a^2 + 2ab + b^2")

    wf = get_assistant_workflow(llm_provider=mock_llm, tool_registry=eval_env["tool_registry"])
    res = wf.handle_user_message("What is (a+b)^2?", active_profile=eval_env["profile"])

    assert res.is_error is False
    assert res.response_mode == "conversational"
    assert len(res.tool_results) == 0
    assert "a^2" in res.response_text


def test_eval_case_3_display_all_tables(eval_env) -> None:
    """Case 3: 'Display all tables.' -> list_database_objects, exact structured output rendered once."""
    mock_llm = MagicMock()
    tc = ToolCall(id="c1", name="list_database_objects", arguments={"object_type": "all", "response_mode": "display"})
    mock_llm.chat.return_value = ChatMessage(role="assistant", tool_calls=(tc,))

    wf = get_assistant_workflow(llm_provider=mock_llm, tool_registry=eval_env["tool_registry"])
    res = wf.handle_user_message("Display all tables.", active_profile=eval_env["profile"])

    assert res.is_error is False
    assert res.response_mode == "display"
    assert len(res.tool_results) == 1
    assert res.tool_results[0].name == "list_database_objects"
    assert res.response_text == ""  # Authoritative display, no duplicate summary
    assert mock_llm.chat.call_count == 1


def test_eval_case_4_describe_orders(eval_env) -> None:
    """Case 4: 'Describe orders' -> describe_object with exact argument 'orders'."""
    mock_llm = MagicMock()
    tc = ToolCall(id="c2", name="describe_object", arguments={"object_name": "orders", "response_mode": "display"})
    mock_llm.chat.return_value = ChatMessage(role="assistant", tool_calls=(tc,))

    wf = get_assistant_workflow(llm_provider=mock_llm, tool_registry=eval_env["tool_registry"])
    res = wf.handle_user_message("Describe orders", active_profile=eval_env["profile"])

    assert res.is_error is False
    assert res.response_mode == "display"
    assert len(res.tool_results) == 1
    assert res.tool_results[0].name == "describe_object"
    assert res.tool_results[0].data["name"] == "orders"
    assert mock_llm.chat.call_count == 1


def test_eval_case_5_show_columns_in_orders(eval_env) -> None:
    """Case 5: 'Show columns in orders' -> describe_object with exact argument 'orders'."""
    mock_llm = MagicMock()
    tc = ToolCall(id="c2", name="describe_object", arguments={"object_name": "orders", "response_mode": "display"})
    mock_llm.chat.return_value = ChatMessage(role="assistant", tool_calls=(tc,))

    wf = get_assistant_workflow(llm_provider=mock_llm, tool_registry=eval_env["tool_registry"])
    res = wf.handle_user_message("Show columns in orders", active_profile=eval_env["profile"])

    assert res.is_error is False
    assert res.response_mode == "display"
    assert len(res.tool_results) == 1
    assert res.tool_results[0].name == "describe_object"
    assert res.tool_results[0].data["name"] == "orders"


def test_eval_case_6_display_orders_clarification(eval_env) -> None:
    """Case 6: 'Display orders' -> Clarify or state row retrieval unavailable."""
    mock_llm = MagicMock()

    wf = get_assistant_workflow(llm_provider=mock_llm, tool_registry=eval_env["tool_registry"])
    res = wf.handle_user_message("Display orders", active_profile=eval_env["profile"])

    assert res.is_error is False
    assert res.response_mode == "conversational"
    assert len(res.tool_results) == 0
    assert "records" in res.response_text.lower() or "structure" in res.response_text.lower()
    # Guardrail intercepts before querying LLM
    assert mock_llm.chat.call_count == 0


def test_eval_case_7_show_rows_from_orders_unavailable(eval_env) -> None:
    """Case 7: 'Show rows from orders' -> State row retrieval unavailable."""
    mock_llm = MagicMock()

    wf = get_assistant_workflow(llm_provider=mock_llm, tool_registry=eval_env["tool_registry"])
    res = wf.handle_user_message("Show rows from orders", active_profile=eval_env["profile"])

    assert res.is_error is False
    assert res.response_mode == "conversational"
    assert len(res.tool_results) == 0
    assert "not currently supported" in res.response_text.lower()
    assert "structure" in res.response_text.lower()
    assert mock_llm.chat.call_count == 0


def test_eval_case_8_display_items_in_orders_no_guessing(eval_env) -> None:
    """Case 8: 'Display items in orders' -> Do not guess order_items; clarify."""
    mock_llm = MagicMock()

    wf = get_assistant_workflow(llm_provider=mock_llm, tool_registry=eval_env["tool_registry"])
    res = wf.handle_user_message("Display the items in orders", active_profile=eval_env["profile"])

    assert res.is_error is False
    assert res.response_mode == "conversational"
    assert len(res.tool_results) == 0
    assert "order_items" in res.response_text.lower()
    assert "record retrieval is not available yet" in res.response_text.lower()
    assert mock_llm.chat.call_count == 0


def test_eval_case_9_show_tables_without_public_formatting(eval_env) -> None:
    """Case 9: 'Show tables without public' -> Deterministic formatting of structured result."""
    mock_llm = MagicMock()
    tc = ToolCall(
        id="c1",
        name="list_database_objects",
        arguments={"presentation_options": {"include_schema": False}, "response_mode": "display"},
    )
    mock_llm.chat.return_value = ChatMessage(role="assistant", tool_calls=(tc,))

    wf = get_assistant_workflow(llm_provider=mock_llm, tool_registry=eval_env["tool_registry"])
    res = wf.handle_user_message("Show tables without public", active_profile=eval_env["profile"])

    assert res.is_error is False
    assert res.response_mode == "display"
    assert len(res.tool_results) == 1
    assert res.presentation_options.get("include_schema") is False
    assert res.presentation_options.get("omit_schema") is True


def test_eval_case_9_reformatting_uses_cached_result(eval_env) -> None:
    """Case 9b: Reformatting already displayed tables does not query database again."""
    mock_llm = MagicMock()
    tc1 = ToolCall(id="c1", name="list_database_objects", arguments={"object_type": "all", "response_mode": "display"})
    tc2 = ToolCall(
        id="c2",
        name="list_database_objects",
        arguments={"presentation_options": {"include_schema": False}, "response_mode": "display"},
    )
    mock_llm.chat.side_effect = [
        ChatMessage(role="assistant", tool_calls=(tc1,)),
        ChatMessage(role="assistant", tool_calls=(tc2,)),
    ]

    wf = get_assistant_workflow(llm_provider=mock_llm, tool_registry=eval_env["tool_registry"])

    # First turn: Display all tables
    res1 = wf.handle_user_message("Display all tables", active_profile=eval_env["profile"])
    assert len(res1.tool_results) == 1
    assert res1.tool_results[0].data.get("cached") is not True

    # Second turn: Reformat without public
    res2 = wf.handle_user_message("Display all the table names without public", active_profile=eval_env["profile"])
    assert res2.response_mode == "display"
    assert res2.presentation_options.get("include_schema") is False
    assert res2.presentation_options.get("omit_schema") is True
    assert len(res2.tool_results) == 1
    # Verify cached data was reused!
    assert res2.tool_results[0].data.get("cached") is True


def test_eval_case_10_why_does_orders_reference_users(eval_env) -> None:
    """Case 10: 'Why does orders reference users?' -> Retrieve relationships, explain evidence."""
    mock_llm = MagicMock()
    tc = ToolCall(id="c4", name="get_relationships", arguments={"response_mode": "explain"})
    tool_msg = ChatMessage(role="assistant", tool_calls=(tc,))
    explain_msg = ChatMessage(
        role="assistant",
        content="The orders table maintains a foreign key 'user_id' referencing the primary key 'id' of the users table.",
    )
    mock_llm.chat.side_effect = [tool_msg, explain_msg]

    wf = get_assistant_workflow(llm_provider=mock_llm, tool_registry=eval_env["tool_registry"])
    res = wf.handle_user_message("Why does orders reference users?", active_profile=eval_env["profile"])

    assert res.is_error is False
    assert res.response_mode == "explain"
    assert len(res.tool_results) == 1
    assert res.tool_results[0].name == "get_relationships"
    assert "foreign key" in res.response_text
    assert mock_llm.chat.call_count == 2


def test_eval_case_11_unknown_table_test_foo(eval_env) -> None:
    """Case 11: 'Show an unknown table named test_foo.' -> Report object not found."""
    mock_llm = MagicMock()
    tc = ToolCall(id="c5", name="describe_object", arguments={"object_name": "test_foo", "response_mode": "display"})
    mock_llm.chat.return_value = ChatMessage(role="assistant", tool_calls=(tc,))

    wf = get_assistant_workflow(llm_provider=mock_llm, tool_registry=eval_env["tool_registry"])
    res = wf.handle_user_message("Show an unknown table named test_foo.", active_profile=eval_env["profile"])

    assert len(res.tool_results) == 1
    assert res.tool_results[0].is_error is True
    assert "does not exist" in res.tool_results[0].data["error"]


def test_eval_case_12_pronoun_context_resolution(eval_env) -> None:
    """Case 12: 'Describe orders' -> 'Show its columns' resolves 'its' using conversation context."""
    mock_llm = MagicMock()
    tc1 = ToolCall(id="c1", name="describe_object", arguments={"object_name": "orders", "response_mode": "display"})
    tc2 = ToolCall(id="c2", name="describe_object", arguments={"object_name": "it", "response_mode": "display"})
    mock_llm.chat.side_effect = [
        ChatMessage(role="assistant", tool_calls=(tc1,)),
        ChatMessage(role="assistant", tool_calls=(tc2,)),
    ]

    wf = get_assistant_workflow(llm_provider=mock_llm, tool_registry=eval_env["tool_registry"])

    # Turn 1: Describe orders
    res1 = wf.handle_user_message("Describe the orders table.", active_profile=eval_env["profile"])
    assert res1.tool_results[0].data["name"] == "orders"

    # Turn 2: Show its columns (model passes "it", workflow resolves to "orders")
    res2 = wf.handle_user_message("Show its columns.", active_profile=eval_env["profile"])
    assert len(res2.tool_results) == 1
    assert res2.tool_results[0].data["name"] == "orders"
    assert res2.tool_results[0].is_error is False


def test_eval_part_b_presentation_options_sequence(eval_env) -> None:
    """Part B.7: Multi-turn sequence verifying structured presentation options and cache reuse."""
    mock_llm = MagicMock()

    # Step 1: Display all tables -> database introspected, schema cached
    tc1 = ToolCall(id="c1", name="list_database_objects", arguments={"object_type": "all", "response_mode": "display"})
    # Step 2: Without mentioning public -> include_schema=False
    tc2 = ToolCall(id="c2", name="list_database_objects", arguments={"presentation_options": {"include_schema": False}, "response_mode": "display"})
    # Step 3: Show only names -> names_only=True
    tc3 = ToolCall(id="c3", name="list_database_objects", arguments={"presentation_options": {"names_only": True}, "response_mode": "display"})
    # Step 4: Number the table names -> numbered=True
    tc4 = ToolCall(id="c4", name="list_database_objects", arguments={"presentation_options": {"numbered": True}, "response_mode": "display"})
    # Step 5: Sort table names alphabetically -> alphabetical=True
    tc5 = ToolCall(id="c5", name="list_database_objects", arguments={"presentation_options": {"alphabetical": True}, "response_mode": "display"})
    # Step 6: Without schema, numbered alphabetically -> include_schema=False, numbered=True, alphabetical=True
    tc6 = ToolCall(
        id="c6",
        name="list_database_objects",
        arguments={"presentation_options": {"include_schema": False, "numbered": True, "alphabetical": True}, "response_mode": "display"},
    )
    # Step 7: Show the tables again -> reuses cache with default formatting
    tc7 = ToolCall(id="c7", name="list_database_objects", arguments={"object_type": "all", "response_mode": "display"})

    mock_llm.chat.side_effect = [
        ChatMessage(role="assistant", tool_calls=(tc1,)),
        ChatMessage(role="assistant", tool_calls=(tc2,)),
        ChatMessage(role="assistant", tool_calls=(tc3,)),
        ChatMessage(role="assistant", tool_calls=(tc4,)),
        ChatMessage(role="assistant", tool_calls=(tc5,)),
        ChatMessage(role="assistant", tool_calls=(tc6,)),
        ChatMessage(role="assistant", tool_calls=(tc7,)),
    ]

    wf = get_assistant_workflow(llm_provider=mock_llm, tool_registry=eval_env["tool_registry"])

    # 1. Display all tables
    r1 = wf.handle_user_message("Display all tables.", active_profile=eval_env["profile"])
    assert r1.response_mode == "display"
    assert len(r1.tool_results) == 1
    assert r1.tool_results[0].data.get("cached") is not True

    # 2. Display without mentioning public
    r2 = wf.handle_user_message("Display all the table names without mentioning public word.", active_profile=eval_env["profile"])
    assert r2.response_mode == "display"
    assert r2.presentation_options.get("include_schema") is False
    assert r2.tool_results[0].data.get("cached") is True

    # 3. Show only names
    r3 = wf.handle_user_message("Show only names.", active_profile=eval_env["profile"])
    assert r3.response_mode == "display"
    assert r3.presentation_options.get("names_only") is True
    assert r3.tool_results[0].data.get("cached") is True

    # 4. Number the table names
    r4 = wf.handle_user_message("Number the table names.", active_profile=eval_env["profile"])
    assert r4.response_mode == "display"
    assert r4.presentation_options.get("numbered") is True
    assert r4.tool_results[0].data.get("cached") is True

    # 5. Sort table names alphabetically
    r5 = wf.handle_user_message("Sort table names alphabetically.", active_profile=eval_env["profile"])
    assert r5.response_mode == "display"
    assert r5.presentation_options.get("alphabetical") is True
    assert r5.tool_results[0].data.get("cached") is True

    # 6. Show all table names without schema names, numbered alphabetically
    r6 = wf.handle_user_message("Show all table names without schema names, numbered alphabetically.", active_profile=eval_env["profile"])
    assert r6.response_mode == "display"
    assert r6.presentation_options.get("include_schema") is False
    assert r6.presentation_options.get("numbered") is True
    assert r6.presentation_options.get("alphabetical") is True
    assert r6.tool_results[0].data.get("cached") is True

    # 7. Show the tables again
    r7 = wf.handle_user_message("Show the tables again.", active_profile=eval_env["profile"])
    assert r7.response_mode == "display"
    assert r7.tool_results[0].data.get("cached") is True

