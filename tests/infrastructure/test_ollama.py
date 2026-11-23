"""Unit tests for Ollama infrastructure adapter."""

import io
import json
import socket
import urllib.error
from unittest.mock import MagicMock, patch
import pytest

from support.application.errors import (
    OllamaConnectionError,
    OllamaModelNotFoundError,
    OllamaResponseError,
    OllamaTimeoutError,
)
from support.domain.assistant import ChatMessage, ToolCall, ToolDefinition
from support.infrastructure.ollama import OllamaClient


def _make_http_response(status: int, data: dict) -> MagicMock:
    body = json.dumps(data).encode("utf-8")
    resp = MagicMock()
    resp.status = status
    resp.read.return_value = body
    resp.__enter__.return_value = resp
    resp.__exit__.return_value = None
    return resp


def test_ollama_client_initialization_defaults() -> None:
    client = OllamaClient(base_url="http://custom:11434/", default_model="custom-coder")
    assert client.base_url == "http://custom:11434"
    assert client.default_model == "custom-coder"


def test_ollama_chat_successful_plain_response() -> None:
    client = OllamaClient(base_url="http://localhost:11434", default_model="qwen-coder")
    messages = [ChatMessage(role="user", content="Hello")]

    mock_resp_data = {
        "model": "qwen-coder",
        "message": {"role": "assistant", "content": "Hello! How can I help?"},
        "done": True,
    }

    with patch("urllib.request.urlopen", return_value=_make_http_response(200, mock_resp_data)):
        res = client.chat(messages)

    assert res.role == "assistant"
    assert res.content == "Hello! How can I help?"
    assert res.tool_calls == ()


def test_ollama_chat_native_tool_calls_parsing() -> None:
    client = OllamaClient()
    messages = [ChatMessage(role="user", content="Show tables")]
    tools = [ToolDefinition(name="list_database_objects", description="List tables")]

    mock_resp_data = {
        "model": "qwen-coder",
        "message": {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": "call_1",
                    "type": "function",
                    "function": {
                        "name": "list_database_objects",
                        "arguments": {"object_type": "table"},
                    },
                }
            ],
        },
    }

    with patch("urllib.request.urlopen", return_value=_make_http_response(200, mock_resp_data)):
        res = client.chat(messages, tools=tools)

    assert len(res.tool_calls) == 1
    tc = res.tool_calls[0]
    assert tc.id == "call_1"
    assert tc.name == "list_database_objects"
    assert tc.arguments["object_type"] == "table"


def test_ollama_chat_embedded_json_tool_calls_parsing() -> None:
    client = OllamaClient()
    messages = [ChatMessage(role="user", content="Describe orders")]
    tools = [ToolDefinition(name="describe_object", description="Describe table")]

    # Model returns JSON in content text instead of tool_calls array
    mock_resp_data = {
        "model": "qwen-coder",
        "message": {
            "role": "assistant",
            "content": '{"name": "describe_object", "arguments": {"object_name": "orders"}}',
        },
    }

    with patch("urllib.request.urlopen", return_value=_make_http_response(200, mock_resp_data)):
        res = client.chat(messages, tools=tools)

    assert len(res.tool_calls) == 1
    tc = res.tool_calls[0]
    assert tc.name == "describe_object"
    assert tc.arguments["object_name"] == "orders"
    assert res.content is None


def test_ollama_chat_tagged_tool_call_parsing() -> None:
    client = OllamaClient()
    messages = [ChatMessage(role="user", content="Show relationships")]
    tools = [ToolDefinition(name="get_relationships", description="Get foreign keys")]

    mock_resp_data = {
        "model": "qwen-coder",
        "message": {
            "role": "assistant",
            "content": '<tool_call>\n{"name": "get_relationships", "arguments": {"table_name": "orders"}}\n</tool_call>',
        },
    }

    with patch("urllib.request.urlopen", return_value=_make_http_response(200, mock_resp_data)):
        res = client.chat(messages, tools=tools)

    assert len(res.tool_calls) == 1
    tc = res.tool_calls[0]
    assert tc.name == "get_relationships"
    assert tc.arguments["table_name"] == "orders"


def test_ollama_chat_strips_pseudo_tool_none() -> None:
    client = OllamaClient()
    messages = [ChatMessage(role="user", content="What is 1+1?")]
    tools = [ToolDefinition(name="list_database_objects", description="List tables")]

    mock_resp_data = {
        "model": "qwen-coder",
        "message": {
            "role": "assistant",
            "content": '{"name": "none", "arguments": {}}',
        },
    }

    with patch("urllib.request.urlopen", return_value=_make_http_response(200, mock_resp_data)):
        res = client.chat(messages, tools=tools)

    # Must NOT treat 'none' as a tool call, and must NOT leave raw JSON in content
    assert len(res.tool_calls) == 0
    assert res.content is None


def test_ollama_chat_embedded_markdown_code_fence() -> None:
    client = OllamaClient()
    messages = [ChatMessage(role="user", content="Describe orders")]
    tools = [ToolDefinition(name="describe_object", description="Describe table")]

    mock_resp_data = {
        "model": "qwen-coder",
        "message": {
            "role": "assistant",
            "content": '```json\n{"name": "describe_object", "arguments": {"object_name": "orders"}}\n```',
        },
    }

    with patch("urllib.request.urlopen", return_value=_make_http_response(200, mock_resp_data)):
        res = client.chat(messages, tools=tools)

    assert len(res.tool_calls) == 1
    assert res.tool_calls[0].name == "describe_object"
    assert res.tool_calls[0].arguments["object_name"] == "orders"
    assert res.content is None


def test_ollama_chat_filters_unknown_native_tool_calls() -> None:
    client = OllamaClient()
    messages = [ChatMessage(role="user", content="hi")]
    tools = [ToolDefinition(name="list_database_objects", description="List tables")]

    mock_resp_data = {
        "model": "qwen-coder",
        "message": {
            "role": "assistant",
            "tool_calls": [
                {"function": {"name": "none", "arguments": {}}},
                {"function": {"name": "unregistered_tool", "arguments": {}}},
            ],
        },
    }

    with patch("urllib.request.urlopen", return_value=_make_http_response(200, mock_resp_data)):
        res = client.chat(messages, tools=tools)

    assert len(res.tool_calls) == 0


def test_ollama_server_connection_refused() -> None:
    client = OllamaClient(base_url="http://127.0.0.1:11434")
    url_err = urllib.error.URLError(ConnectionRefusedError("Connection refused"))

    with patch("urllib.request.urlopen", side_effect=url_err):
        with pytest.raises(OllamaConnectionError) as exc_info:
            client.chat([ChatMessage(role="user", content="hi")])
    assert "Cannot connect to local Ollama server" in str(exc_info.value)


def test_ollama_timeout_error() -> None:
    client = OllamaClient(base_url="http://127.0.0.1:11434", timeout=5.0)
    timeout_err = TimeoutError("The read operation timed out")

    with patch("urllib.request.urlopen", side_effect=timeout_err):
        with pytest.raises(OllamaTimeoutError) as exc_info:
            client.chat([ChatMessage(role="user", content="hi")])
    assert "Timed out connecting to Ollama server" in str(exc_info.value)


def test_ollama_model_not_found_404() -> None:
    client = OllamaClient()
    http_err = urllib.error.HTTPError(
        url="http://localhost:11434/api/chat",
        code=404,
        msg="Not Found",
        hdrs={},  # type: ignore[arg-type]
        fp=io.BytesIO(b'{"error": "model \'missing-model\' not found"}'),
    )

    with patch("urllib.request.urlopen", side_effect=http_err):
        with pytest.raises(OllamaModelNotFoundError) as exc_info:
            client.chat([ChatMessage(role="user", content="hi")])
    assert "not found" in str(exc_info.value).lower()


def test_ollama_invalid_json_response() -> None:
    client = OllamaClient()
    mock_resp = MagicMock()
    mock_resp.read.return_value = b"<html>502 Bad Gateway</html>"
    mock_resp.__enter__.return_value = mock_resp
    mock_resp.__exit__.return_value = None

    with patch("urllib.request.urlopen", return_value=mock_resp):
        with pytest.raises(OllamaResponseError) as exc_info:
            client.chat([ChatMessage(role="user", content="hi")])
    assert "invalid JSON" in str(exc_info.value)


def test_ollama_check_model_status_available_with_tools() -> None:
    client = OllamaClient(default_model="qwen2.5-coder:3b")

    tags_data = {
        "models": [
            {"name": "qwen2.5-coder:3b", "size": 12345},
            {"name": "llama3:latest", "size": 67890},
        ]
    }
    show_data = {
        "capabilities": ["completion", "tools"]
    }

    with patch("urllib.request.urlopen") as mock_urlopen:
        mock_urlopen.side_effect = [
            _make_http_response(200, tags_data),
            _make_http_response(200, show_data),
        ]
        info = client.check_model_status()

    assert info.name == "qwen2.5-coder:3b"
    assert info.is_available is True
    assert info.supports_tools is True
    assert info.details == "Available"


def test_ollama_check_model_status_not_installed() -> None:
    client = OllamaClient(default_model="deepseek-coder:6.7b")

    tags_data = {"models": [{"name": "qwen2.5-coder:3b"}]}

    with patch("urllib.request.urlopen", return_value=_make_http_response(200, tags_data)):
        info = client.check_model_status()

    assert info.name == "deepseek-coder:6.7b"
    assert info.is_available is False
    assert "not installed" in (info.details or "")


def test_ollama_check_model_status_server_offline() -> None:
    client = OllamaClient(default_model="qwen2.5-coder:3b")
    url_err = urllib.error.URLError(ConnectionRefusedError("Offline"))

    with patch("urllib.request.urlopen", side_effect=url_err):
        info = client.check_model_status()

    assert info.is_available is False
    assert "offline" in (info.details or "").lower()


def test_ollama_list_models() -> None:
    client = OllamaClient()
    tags_data = {
        "models": [
            {"name": "qwen2.5-coder:3b", "size": 1000},
            {"name": "qwen2.5-coder:7b", "size": 2000},
        ]
    }

    with patch("urllib.request.urlopen", return_value=_make_http_response(200, tags_data)):
        models = client.list_models()

    assert len(models) == 2
    assert models[0].name == "qwen2.5-coder:3b"
    assert models[1].name == "qwen2.5-coder:7b"


def test_ollama_chat_strips_tool_response_tags_and_raw_json() -> None:
    client = OllamaClient()
    mock_resp_data = {
        "model": "qwen2.5-coder:7b",
        "message": {
            "role": "assistant",
            "content": (
                "<tool_response>\n"
                '{"table": "public.orders", "columns": [{"name": "id"}]}\n'
                "</tool_response>\n"
                "The orders table contains 1 column."
            ),
        },
    }

    with patch("urllib.request.urlopen", return_value=_make_http_response(200, mock_resp_data)):
        res = client.chat([ChatMessage(role="user", content="Explain orders")])

    assert "<tool_response>" not in (res.content or "")
    assert "</tool_response>" not in (res.content or "")
    assert "public.orders" not in (res.content or "")
    assert "The orders table contains 1 column." in (res.content or "")


def test_ollama_chat_strips_standalone_protocol_json() -> None:
    client = OllamaClient()
    mock_resp_data = {
        "model": "qwen2.5-coder:7b",
        "message": {
            "role": "assistant",
            "content": '<tool_response>\n{"table": "orders"}\n</tool_response>',
        },
    }

    with patch("urllib.request.urlopen", return_value=_make_http_response(200, mock_resp_data)):
        res = client.chat([ChatMessage(role="user", content="orders")])

    # Stripped completely, not exposing protocol JSON as fallback
    assert res.content is None


def test_ollama_chat_preserves_math_and_natural_text() -> None:
    client = OllamaClient()
    mock_resp_data = {
        "model": "qwen2.5-coder:7b",
        "message": {
            "role": "assistant",
            "content": "(a+b)^2 = a^2 + 2ab + b^2. Also note that 3 < 5 and 5 > 2.",
        },
    }

    with patch("urllib.request.urlopen", return_value=_make_http_response(200, mock_resp_data)):
        res = client.chat([ChatMessage(role="user", content="What is (a+b)^2?")])

    assert res.content == "(a+b)^2 = a^2 + 2ab + b^2. Also note that 3 < 5 and 5 > 2."
