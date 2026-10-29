"""Tests for LLM gateway providers, configuration, and service."""

from pathlib import Path
import pytest
import httpx

from support.core.exceptions import (
    GenerationError,
    LLMAuthenticationError,
    ModelNotFoundError,
    ProviderNotConfiguredError,
    ProviderUnreachableError,
)
from support.llm.config import LLMConfig, LLMConfigManager
from support.llm.models import (
    GenerationSettings,
    ProviderStatus,
)
from support.llm.providers.gemini import GeminiProvider
from support.llm.providers.grok import GrokProvider
from support.llm.providers.ollama import OllamaProvider
from support.llm.service import LLMService


# --- Ollama Tests ---


def test_ollama_provider_validation_success():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/tags"
        return httpx.Response(
            200,
            json={
                "models": [
                    {"name": "qwen2.5-coder:7b", "size": 4000000000, "details": {"family": "qwen2"}}
                ]
            },
        )

    client = httpx.Client(transport=httpx.MockTransport(handler), base_url="http://test:11434")
    provider = OllamaProvider(client=client, default_model="qwen2.5-coder:7b")

    result = provider.validate_configuration()
    assert result.status == ProviderStatus.AVAILABLE
    assert "available" in result.message.lower()


def test_ollama_provider_validation_model_missing():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"models": [{"name": "other-model:latest"}]})

    client = httpx.Client(transport=httpx.MockTransport(handler), base_url="http://test:11434")
    provider = OllamaProvider(client=client, default_model="qwen2.5-coder:7b")

    result = provider.validate_configuration()
    assert result.status == ProviderStatus.MODEL_UNAVAILABLE
    assert "not installed" in result.message.lower()
    assert "ollama pull" in result.hint


def test_ollama_provider_validation_unreachable():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("Connection refused")

    client = httpx.Client(transport=httpx.MockTransport(handler), base_url="http://test:11434")
    provider = OllamaProvider(client=client)

    result = provider.validate_configuration()
    assert result.status == ProviderStatus.UNREACHABLE
    assert "not running" in result.message.lower()


def test_ollama_provider_generate_success():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/generate"
        return httpx.Response(
            200,
            json={
                "response": "SELECT * FROM users;",
                "prompt_eval_count": 12,
                "eval_count": 8,
                "total_duration": 50000000,  # 50 ms
                "done_reason": "stop",
            },
        )

    client = httpx.Client(transport=httpx.MockTransport(handler), base_url="http://test:11434")
    provider = OllamaProvider(client=client, default_model="qwen2.5-coder:7b")

    resp = provider.generate("generate a query")
    assert resp.text == "SELECT * FROM users;"
    assert resp.provider == "ollama"
    assert resp.model == "qwen2.5-coder:7b"
    assert resp.usage.prompt_tokens == 12
    assert resp.usage.completion_tokens == 8
    assert resp.latency_ms == 50.0


def test_ollama_provider_generate_model_not_found():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, text="model not found")

    client = httpx.Client(transport=httpx.MockTransport(handler), base_url="http://test:11434")
    provider = OllamaProvider(client=client)

    with pytest.raises(ModelNotFoundError):
        provider.generate("test prompt", model="missing-model")


# --- Grok Tests ---


def test_grok_provider_not_configured():
    provider = GrokProvider(api_key=None)
    result = provider.validate_configuration()
    assert result.status == ProviderStatus.NOT_CONFIGURED
    assert "XAI_API_KEY" in result.hint

    with pytest.raises(ProviderNotConfiguredError):
        provider.generate("test")


def test_grok_provider_auth_failure():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers.get("Authorization") == "Bearer invalid-key"
        return httpx.Response(401, json={"error": "unauthorized"})

    client = httpx.Client(transport=httpx.MockTransport(handler), base_url="https://api.xai.com/v1")
    provider = GrokProvider(api_key="invalid-key", client=client)

    result = provider.validate_configuration()
    assert result.status == ProviderStatus.AUTH_FAILED
    assert "401" in result.message

    with pytest.raises(LLMAuthenticationError):
        provider.generate("test")


def test_grok_provider_generate_success():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers.get("Authorization") == "Bearer valid-key"
        if "models" in request.url.path:
            return httpx.Response(
                200,
                json={"data": [{"id": "grok-2-latest"}, {"id": "grok-beta"}]},
            )
        if "chat/completions" in request.url.path:
            return httpx.Response(
                200,
                json={
                    "choices": [
                        {
                            "message": {"content": "Hello from Grok!"},
                            "finish_reason": "stop",
                        }
                    ],
                    "usage": {
                        "prompt_tokens": 15,
                        "completion_tokens": 5,
                        "total_tokens": 20,
                    },
                },
            )
        return httpx.Response(404, json={"error": "not found"})

    client = httpx.Client(transport=httpx.MockTransport(handler), base_url="https://api.xai.com/v1")
    provider = GrokProvider(api_key="valid-key", client=client)

    result = provider.validate_configuration()
    assert result.status == ProviderStatus.AVAILABLE

    resp = provider.generate("hello", model="grok-2-latest")
    assert resp.text == "Hello from Grok!"
    assert resp.provider == "grok"
    assert resp.model == "grok-2-latest"
    assert resp.usage.total_tokens == 20


# --- Gemini Tests ---


def test_gemini_provider_not_configured():
    provider = GeminiProvider(api_key=None)
    result = provider.validate_configuration()
    assert result.status == ProviderStatus.NOT_CONFIGURED
    assert "GEMINI_API_KEY" in result.hint

    with pytest.raises(ProviderNotConfiguredError):
        provider.generate("test")


def test_gemini_provider_auth_failure():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, json={"error": {"message": "API key not valid."}})

    client = httpx.Client(transport=httpx.MockTransport(handler), base_url="https://test")
    provider = GeminiProvider(api_key="bad-key", client=client)

    result = provider.validate_configuration()
    assert result.status == ProviderStatus.AUTH_FAILED

    with pytest.raises(LLMAuthenticationError):
        provider.generate("test")


def test_gemini_provider_generate_success():
    def handler(request: httpx.Request) -> httpx.Response:
        assert "generateContent" in request.url.path
        assert request.headers.get("x-goog-api-key") == "valid-key"
        return httpx.Response(
            200,
            json={
                "candidates": [
                    {
                        "content": {
                            "parts": [{"text": "Hello from Gemini!"}]
                        },
                        "finishReason": "STOP",
                    }
                ],
                "usageMetadata": {
                    "promptTokenCount": 10,
                    "candidatesTokenCount": 6,
                    "totalTokenCount": 16,
                },
            },
        )

    client = httpx.Client(transport=httpx.MockTransport(handler), base_url="https://test")
    provider = GeminiProvider(api_key="valid-key", client=client)

    resp = provider.generate("hello", model="gemini-2.0-flash")
    assert resp.text == "Hello from Gemini!"
    assert resp.provider == "gemini"
    assert resp.model == "gemini-2.0-flash"
    assert resp.usage.total_tokens == 16


# --- LLMConfigManager & LLMService Tests ---


def test_llm_config_manager(tmp_path: Path):
    cfg_mgr = LLMConfigManager(config_dir=tmp_path)
    config = cfg_mgr.load_config()
    assert config.default_provider == "ollama"

    # Mutate and save
    cfg_mgr.set_active_provider_and_model("gemini", "gemini-1.5-pro")
    updated = cfg_mgr.load_config()
    assert updated.default_provider == "gemini"
    assert updated.default_model == "gemini-1.5-pro"


def test_llm_service_override_provider_without_mutating_config(tmp_path: Path):
    cfg_mgr = LLMConfigManager(config_dir=tmp_path)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"response": "ollama reply"})

    client = httpx.Client(transport=httpx.MockTransport(handler), base_url="http://test")
    custom_ollama = OllamaProvider(client=client)

    service = LLMService(
        config_manager=cfg_mgr,
        custom_providers={"ollama": custom_ollama},
    )

    # Config is default ollama
    assert service.get_config().default_provider == "ollama"

    # Call with explicit model
    res = service.generate("test prompt", provider="ollama", model="qwen2.5:0.5b")
    assert res.text == "ollama reply"
    assert res.model == "qwen2.5:0.5b"

    # Persistent config remains unchanged
    assert service.get_config().default_model == "qwen2.5-coder:7b"
