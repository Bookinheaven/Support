"""Unit tests for conversational assistant workflow and bounded execution cycle."""

from unittest.mock import MagicMock
import pytest

from support.application.assistant import AssistantWorkflow
from support.application.tools import ToolRegistry, ToolResult
from support.domain.assistant import ChatMessage, ToolCall, ToolDefinition


@pytest.fixture
def mock_tool_registry():
    registry = MagicMock(spec=ToolRegistry)
    registry.get_tool_definitions.return_value = [
        ToolDefinition("list_database_objects", "List tables"),
        ToolDefinition("describe_object", "Describe table"),
    ]
    return registry


def test_assistant_simple_conversational_turn_no_tools(mock_tool_registry) -> None:
    mock_llm = MagicMock()
    mock_llm.chat.return_value = ChatMessage(role="assistant", content="Hello! How can I assist you with your database?")

    wf = AssistantWorkflow(llm_provider=mock_llm, tool_registry=mock_tool_registry)
    result = wf.handle_user_message("Hello there", active_profile="test-db")

    assert result.is_error is False
    assert result.response_text == "Hello! How can I assist you with your database?"
    assert result.tool_results == ()
    assert len(wf.history) == 2  # user message, assistant message
    assert wf.history[0].role == "user"
    assert wf.history[0].content == "Hello there"
    assert wf.history[1].role == "assistant"


def test_assistant_direct_retrieval_path_no_second_model_call(mock_tool_registry) -> None:
    """Path B: Exact data retrieval executes the tool once and stops without duplicate LLM summary."""
    mock_llm = MagicMock()

    # Model calls tool list_database_objects
    tc = ToolCall(id="call_list", name="list_database_objects", arguments={"object_type": "all", "response_mode": "display"})
    tool_msg = ChatMessage(role="assistant", tool_calls=(tc,))
    mock_llm.chat.return_value = tool_msg

    mock_tool_registry.execute_tool.return_value = ToolResult(
        name="list_database_objects",
        call_id="call_list",
        data={"tables": ["users", "orders"], "count": 2},
        response_mode="display",
    )

    wf = AssistantWorkflow(llm_provider=mock_llm, tool_registry=mock_tool_registry)
    result = wf.handle_user_message("Display all tables", active_profile="test-db")

    assert result.is_error is False
    assert result.response_mode == "display"
    assert result.response_text == ""  # No redundant text summary
    assert len(result.tool_results) == 1
    assert result.tool_results[0].name == "list_database_objects"

    # Crucial: LLM chat was called ONLY ONCE
    assert mock_llm.chat.call_count == 1

    # Verify tool registry was executed
    mock_tool_registry.execute_tool.assert_called_once_with(
        tool_name="list_database_objects",
        arguments={"object_type": "all", "response_mode": "display"},
        active_profile="test-db",
        call_id="call_list",
    )

    # History contains user -> assistant(tool_call) -> tool(result)
    roles = [m.role for m in wf.history]
    assert roles == ["user", "assistant", "tool"]


def test_assistant_retrieval_followed_by_explanation_cycle(mock_tool_registry) -> None:
    """Path C: Explanatory queries retrieve tool data and formulate a conversational explanation."""
    mock_llm = MagicMock()

    # Turn 1: Model calls tool get_relationships with response_mode='explain'
    tc = ToolCall(id="call_rel", name="get_relationships", arguments={"table_name": "orders", "response_mode": "explain"})
    tool_msg = ChatMessage(role="assistant", tool_calls=(tc,))

    # Turn 2: After tool execution, Model explains the relationship
    explanation_msg = ChatMessage(role="assistant", content="The orders.user_id column references users.id.")
    mock_llm.chat.side_effect = [tool_msg, explanation_msg]

    mock_tool_registry.execute_tool.return_value = ToolResult(
        name="get_relationships",
        call_id="call_rel",
        data={"relationships": [{"source": "orders.user_id", "target": "users.id"}]},
        response_mode="explain",
    )

    wf = AssistantWorkflow(llm_provider=mock_llm, tool_registry=mock_tool_registry)
    result = wf.handle_user_message("Why does the orders table reference users?", active_profile="test-db")

    assert result.is_error is False
    assert result.response_mode == "explain"
    assert result.response_text == "The orders.user_id column references users.id."
    assert len(result.tool_results) == 1

    # LLM chat was called twice (turn 1 for tool call, turn 2 for explanation)
    assert mock_llm.chat.call_count == 2

    # History contains user -> assistant(tool_call) -> tool(result) -> assistant(explanation)
    roles = [m.role for m in wf.history]
    assert roles == ["user", "assistant", "tool", "assistant"]


def test_assistant_conversational_correction_on_empty_content(mock_tool_registry) -> None:
    """Path A: If model returns empty content, assistant performs one controlled correction attempt."""
    mock_llm = MagicMock()
    # Turn 1: empty content; Turn 2 (correction): valid answer
    mock_llm.chat.side_effect = [
        ChatMessage(role="assistant", content=""),
        ChatMessage(role="assistant", content="1 + 1 equals 2."),
    ]

    wf = AssistantWorkflow(llm_provider=mock_llm, tool_registry=mock_tool_registry)
    result = wf.handle_user_message("What is 1 + 1?", active_profile="test-db")

    assert result.is_error is False
    assert result.response_mode == "conversational"
    assert result.response_text == "1 + 1 equals 2."
    assert result.tool_results == ()
    assert mock_llm.chat.call_count == 2


def test_assistant_conversational_error_when_correction_fails(mock_tool_registry) -> None:
    """Path A: If model still returns empty content after correction, produces clean error."""
    mock_llm = MagicMock()
    mock_llm.chat.side_effect = [
        ChatMessage(role="assistant", content=""),
        ChatMessage(role="assistant", content=""),
    ]

    wf = AssistantWorkflow(llm_provider=mock_llm, tool_registry=mock_tool_registry)
    result = wf.handle_user_message("What is 1 + 1?", active_profile="test-db")

    assert result.is_error is True
    assert "did not provide a conversational response" in result.error_message
    assert result.response_text == ""


def test_assistant_repeated_tool_call_loop_protection(mock_tool_registry) -> None:
    mock_llm = MagicMock()

    # Model tries to call the exact same tool with the exact same arguments repeatedly in one turn
    tc = ToolCall(id="call_dup", name="describe_object", arguments={"object_name": "users", "response_mode": "explain"})
    repeat_tool_msg = ChatMessage(role="assistant", tool_calls=(tc,))
    final_msg = ChatMessage(role="assistant", content="I stopped after repeating the call.")

    mock_llm.chat.side_effect = [repeat_tool_msg, repeat_tool_msg, final_msg]

    mock_tool_registry.execute_tool.return_value = ToolResult(
        name="describe_object",
        call_id="call_dup",
        data={"name": "users"},
        response_mode="explain",
    )

    wf = AssistantWorkflow(llm_provider=mock_llm, tool_registry=mock_tool_registry)
    result = wf.handle_user_message("Explain the users table", active_profile="test-db")

    assert result.is_error is False
    assert len(result.tool_results) == 2
    # The second tool call should report repeated call prevention
    assert result.tool_results[1].is_error is True
    assert "already executed" in result.tool_results[1].data["error"]


def test_assistant_multi_turn_conversation_context(mock_tool_registry) -> None:
    mock_llm = MagicMock()
    mock_llm.chat.side_effect = [
        ChatMessage(role="assistant", content="The users table has id, name, and email."),
        ChatMessage(role="assistant", content="Yes, id is the primary key."),
    ]

    wf = AssistantWorkflow(llm_provider=mock_llm, tool_registry=mock_tool_registry)

    # Turn 1
    res1 = wf.handle_user_message("Tell me about users", active_profile="test-db")
    assert "users table" in res1.response_text

    # Turn 2
    res2 = wf.handle_user_message("Does it have a primary key?", active_profile="test-db")
    assert "primary key" in res2.response_text

    # Conversation history persists across turns
    assert len(wf.history) == 4
    assert wf.history[0].content == "Tell me about users"
    assert wf.history[2].content == "Does it have a primary key?"


def test_assistant_conversation_reset(mock_tool_registry) -> None:
    mock_llm = MagicMock()
    mock_llm.chat.return_value = ChatMessage(role="assistant", content="Answer")

    wf = AssistantWorkflow(llm_provider=mock_llm, tool_registry=mock_tool_registry)
    wf.handle_user_message("Question", active_profile="test-db")
    assert len(wf.history) == 2

    wf.reset_conversation()
    assert len(wf.history) == 0


def test_assistant_no_arbitrary_execution() -> None:
    # Security test: ensure model-generated tool calls for arbitrary actions are rejected
    mock_llm = MagicMock()
    tc_hack = ToolCall(id="hack_1", name="execute_bash", arguments={"cmd": "rm -rf /"})
    mock_llm.chat.side_effect = [
        ChatMessage(role="assistant", tool_calls=(tc_hack,)),
        ChatMessage(role="assistant", content="Command failed."),
    ]

    mock_registry = ToolRegistry(schema_workflow=MagicMock(), connection_workflow=MagicMock())
    wf = AssistantWorkflow(llm_provider=mock_llm, tool_registry=mock_registry)
    result = wf.handle_user_message("Run bash command", active_profile="test-db")

    assert len(result.tool_results) == 1
    assert result.tool_results[0].is_error is True
    assert "Unknown tool" in result.tool_results[0].data["error"]
