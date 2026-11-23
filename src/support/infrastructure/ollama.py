"""Ollama local language model adapter and native tool-calling client."""

from __future__ import annotations

import json
import os
import re
import socket
import urllib.error
import urllib.request
from typing import Any

from support.application.errors import (
    OllamaConnectionError,
    OllamaModelNotFoundError,
    OllamaResponseError,
    OllamaTimeoutError,
    OllamaToolCallingUnsupportedError,
)
from support.application.ports import LLMProvider
from support.domain.assistant import ChatMessage, ModelInfo, ToolCall, ToolDefinition
from support.infrastructure.config import get_assistant_model, get_ollama_host

DEFAULT_OLLAMA_TIMEOUT: float = 30.0


class OllamaClient(LLMProvider):
    """Infrastructure client for communicating with local Ollama daemon via native REST API."""

    def __init__(
        self,
        base_url: str | None = None,
        default_model: str | None = None,
        timeout: float = DEFAULT_OLLAMA_TIMEOUT,
    ) -> None:
        self._base_url = (base_url or get_ollama_host()).rstrip("/")
        self._default_model = default_model or get_assistant_model()
        self._timeout = timeout

    @property
    def base_url(self) -> str:
        """Configured base URL of the Ollama server."""
        return self._base_url

    @property
    def default_model(self) -> str | None:
        """Configured default assistant model tag."""
        return self._default_model

    def _request(
        self,
        endpoint: str,
        data: dict[str, Any] | None = None,
        method: str = "GET",
    ) -> dict[str, Any]:
        """Send HTTP request to Ollama endpoint with comprehensive error translation."""
        url = f"{self._base_url}{endpoint}"
        headers = {"Accept": "application/json"}
        payload_bytes = None

        if data is not None:
            headers["Content-Type"] = "application/json"
            payload_bytes = json.dumps(data).encode("utf-8")
            method = "POST"

        req = urllib.request.Request(url, data=payload_bytes, headers=headers, method=method)

        try:
            with urllib.request.urlopen(req, timeout=self._timeout) as resp:
                body = resp.read().decode("utf-8")
                return json.loads(body) if body else {}
        except urllib.error.HTTPError as exc:
            err_body = ""
            try:
                err_body = exc.read().decode("utf-8")
            except Exception:
                pass

            if exc.code == 404:
                raise OllamaModelNotFoundError(
                    f"Resource or model not found on Ollama server at '{self._base_url}'. "
                    f"Details: {err_body or exc.reason}"
                ) from exc
            raise OllamaResponseError(
                f"Ollama server returned HTTP {exc.code}: {err_body or exc.reason}"
            ) from exc
        except (urllib.error.URLError, ConnectionRefusedError, socket.error) as exc:
            reason = getattr(exc, "reason", str(exc))
            if isinstance(reason, (TimeoutError, socket.timeout)) or "timed out" in str(reason).lower():
                raise OllamaTimeoutError(
                    f"Timed out connecting to Ollama server at '{self._base_url}' after {self._timeout}s."
                ) from exc
            raise OllamaConnectionError(
                f"Cannot connect to local Ollama server at '{self._base_url}'. "
                "Ensure Ollama is running (e.g. 'ollama serve' or system service)."
            ) from exc
        except (TimeoutError, socket.timeout) as exc:
            raise OllamaTimeoutError(
                f"Timed out connecting to Ollama server at '{self._base_url}' after {self._timeout}s."
            ) from exc
        except json.JSONDecodeError as exc:
            raise OllamaResponseError(f"Ollama server returned invalid JSON: {exc}") from exc

    def check_model_status(self, model: str | None = None) -> ModelInfo:
        """Inspect local model availability and native tool calling capabilities."""
        target_model = (model or self._default_model or "").strip()
        if not target_model:
            return ModelInfo(
                name="",
                is_available=False,
                supports_tools=False,
                details="Not configured",
            )
        try:
            tags_resp = self._request("/api/tags")
        except OllamaConnectionError:
            return ModelInfo(
                name=target_model,
                is_available=False,
                supports_tools=False,
                details=f"Ollama server offline at {self._base_url}",
            )
        except Exception as exc:
            return ModelInfo(
                name=target_model,
                is_available=False,
                supports_tools=False,
                details=f"Ollama error: {exc}",
            )

        models = tags_resp.get("models", [])
        installed_names = [m.get("name", "") for m in models]

        # Match exact name or base tag without :latest
        matching_model = None
        for m in models:
            m_name = m.get("name", "")
            if m_name == target_model:
                matching_model = m
                break
            if ":" not in target_model and m_name == f"{target_model}:latest":
                matching_model = m
                break
            if target_model.endswith(":latest") and m_name == target_model[:-7]:
                matching_model = m
                break

        if not matching_model:
            return ModelInfo(
                name=target_model,
                is_available=False,
                supports_tools=False,
                details=f"Model '{target_model}' is not installed locally. Run: ollama run {target_model}",
            )

        # Inspect model capabilities for tool calling
        supports_tools = True
        try:
            show_resp = self._request("/api/show", data={"name": matching_model.get("name", target_model)})
            capabilities = show_resp.get("capabilities", [])
            if capabilities and "tools" not in capabilities:
                supports_tools = False
        except Exception:
            # If show endpoint fails, assume tools capability from model details or default
            supports_tools = True

        return ModelInfo(
            name=matching_model.get("name", target_model),
            is_available=True,
            supports_tools=supports_tools,
            details="Available",
        )

    def list_models(self) -> list[ModelInfo]:
        """List all locally installed models on the Ollama instance."""
        try:
            tags_resp = self._request("/api/tags")
            models = tags_resp.get("models", [])
            results: list[ModelInfo] = []
            for m in models:
                name = m.get("name", "")
                results.append(
                    ModelInfo(
                        name=name,
                        is_available=True,
                        supports_tools=True,
                        details=f"Size: {m.get('size', 0)} bytes",
                    )
                )
            return results
        except Exception:
            return []

    def chat(
        self,
        messages: list[ChatMessage],
        tools: list[ToolDefinition] | None = None,
        model: str | None = None,
    ) -> ChatMessage:
        """Send chat request with native tool calling to Ollama."""
        target_model = model or self._default_model

        payload: dict[str, Any] = {
            "model": target_model,
            "messages": [m.to_dict() for m in messages],
            "stream": False,
        }

        tool_names = set()
        if tools:
            payload["tools"] = [t.to_dict() for t in tools]
            tool_names = {t.name for t in tools}

        resp = self._request("/api/chat", data=payload)
        msg_data = resp.get("message", {})
        role = msg_data.get("role", "assistant")
        content = msg_data.get("content")

        tool_calls: list[ToolCall] = []

        # 1. Parse native tool_calls list if returned by Ollama
        raw_tool_calls = msg_data.get("tool_calls", [])
        if raw_tool_calls:
            for i, tc in enumerate(raw_tool_calls):
                fn = tc.get("function", {})
                name = (fn.get("name") or "").strip()
                # Reject invalid, empty, or pseudo-tool names like 'none'
                if not name or name.lower() in ("none", "null", "undefined"):
                    continue
                # Reject unregistered tool names
                if tools and name not in tool_names:
                    continue
                args = fn.get("arguments", {})
                if isinstance(args, str):
                    try:
                        args = json.loads(args)
                    except json.JSONDecodeError:
                        args = {}
                if not isinstance(args, dict):
                    args = {}
                call_id = tc.get("id") or f"call_{name}_{i}"
                tool_calls.append(ToolCall(id=call_id, name=name, arguments=args))

        # 2. Extract embedded tool calls or strip pseudo-tool JSON if present in content
        if content:
            extracted_calls, cleaned_content = self._extract_embedded_tool_calls(content, tool_names)
            if extracted_calls and not tool_calls:
                tool_calls.extend(extracted_calls)
            content = cleaned_content

        return ChatMessage(
            role=role,
            content=content,
            tool_calls=tuple(tool_calls),
        )

    def _extract_embedded_tool_calls(
        self,
        content: str,
        valid_tool_names: set[str],
    ) -> tuple[list[ToolCall], str | None]:
        """Extract tool calls and strip pseudo-tool JSON and protocol leakage from message text."""
        tool_calls: list[ToolCall] = []

        # 1. Strip all <tool_response>...</tool_response> blocks and dangling tags
        cleaned = re.sub(r"<tool_response>[\s\S]*?</tool_response>", "", content)
        cleaned = re.sub(r"</?tool_response>", "", cleaned)
        cleaned = re.sub(r"</?tool_call>", "", cleaned)

        # 2. Extract embedded tool calls or strip JSON protocol objects
        decoder = json.JSONDecoder()
        pos = 0
        cleaned_spans: list[tuple[int, int]] = []

        while pos < len(cleaned):
            idx = cleaned.find("{", pos)
            if idx == -1:
                break
            try:
                obj, end_pos = decoder.raw_decode(cleaned, idx)
                if isinstance(obj, dict):
                    # Check if it's a tool call object: {"name": ..., "arguments": ...}
                    if "name" in obj:
                        name = str(obj.get("name") or "").strip()
                        span_start = idx
                        span_end = end_pos

                        # Expand span to remove surrounding markdown code fences
                        before = cleaned[:span_start].rstrip()
                        if before.endswith("```json") or before.endswith("```"):
                            span_start = before.rfind("```")

                        after = cleaned[span_end:].lstrip()
                        if after.startswith("```"):
                            span_end += (len(cleaned[span_end:]) - len(after)) + 3

                        cleaned_spans.append((span_start, span_end))

                        # Only register tool call if tool is registered
                        if name in valid_tool_names:
                            args = obj.get("arguments", {})
                            if isinstance(args, str):
                                try:
                                    args = json.loads(args)
                                except Exception:
                                    args = {}
                            if not isinstance(args, dict):
                                args = {}
                            call_id = f"call_{name}_{len(tool_calls)}"
                            tool_calls.append(ToolCall(id=call_id, name=name, arguments=args))

                    # Check if it's a raw database protocol payload: e.g. {"table": ...}, {"columns": ...}
                    elif any(k in obj for k in ("table", "columns", "tables", "relationships", "database", "error")):
                        span_start = idx
                        span_end = end_pos
                        before = cleaned[:span_start].rstrip()
                        if before.endswith("```json") or before.endswith("```"):
                            span_start = before.rfind("```")
                        after = cleaned[span_end:].lstrip()
                        if after.startswith("```"):
                            span_end += (len(cleaned[span_end:]) - len(after)) + 3
                        cleaned_spans.append((span_start, span_end))

                pos = end_pos
            except json.JSONDecodeError:
                pos = idx + 1

        if cleaned_spans:
            sorted_spans = sorted(cleaned_spans, key=lambda s: s[0])
            result_chars: list[str] = []
            last_idx = 0
            for start, end in sorted_spans:
                if start > last_idx:
                    result_chars.append(cleaned[last_idx:start])
                last_idx = max(last_idx, end)
            if last_idx < len(cleaned):
                result_chars.append(cleaned[last_idx:])
            remaining = "".join(result_chars).strip()
        else:
            remaining = cleaned.strip()

        if remaining in ("```", "```json", "```json\n```", "```\n```"):
            remaining = ""

        return tool_calls, (remaining if remaining else None)
