"""xAI Grok LLM provider adapter."""

import os
import time
from typing import Optional
import httpx

from support.core.exceptions import (
    GenerationError,
    LLMAuthenticationError,
    ModelNotFoundError,
    ProviderNotConfiguredError,
    ProviderUnreachableError,
)
from support.llm.base import BaseLLMProvider
from support.llm.credentials import CredentialManager
from support.llm.models import (
    GenerationResponse,
    GenerationSettings,
    GenerationUsage,
    ModelInfo,
    ProviderStatus,
    ProviderValidationResult,
)


class GrokProvider(BaseLLMProvider):
    """Adapter for xAI Grok API."""

    def __init__(
        self,
        api_key: Optional[str] = None,
        base_url: Optional[str] = None,
        default_model: str = "grok-2-latest",
        client: Optional[httpx.Client] = None,
    ) -> None:
        self.api_key = api_key or CredentialManager.get_api_key("grok")
        self.base_url = (
            base_url
            or os.getenv("XAI_BASE_URL")
            or "https://api.xai.com/v1"
        ).rstrip("/")
        self.default_model = default_model
        self._client = client

    @property
    def provider_type(self) -> str:
        return "grok"

    def _get_headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        return headers

    def _send_get(self, path: str, timeout: float = 5.0) -> httpx.Response:
        client = self._client or httpx.Client(base_url=self.base_url, timeout=timeout)
        should_close = self._client is None
        try:
            return client.get(path, headers=self._get_headers())
        finally:
            if should_close:
                client.close()

    def _send_post(self, path: str, payload: dict, timeout: float = 30.0) -> httpx.Response:
        client = self._client or httpx.Client(base_url=self.base_url, timeout=timeout)
        should_close = self._client is None
        try:
            return client.post(path, json=payload, headers=self._get_headers())
        finally:
            if should_close:
                client.close()

    def validate_configuration(
        self, model: Optional[str] = None
    ) -> ProviderValidationResult:
        """Validate xAI API key and connectivity."""
        target_model = model or self.default_model

        if not self.api_key:
            return ProviderValidationResult(
                provider=self.provider_type,
                status=ProviderStatus.NOT_CONFIGURED,
                message="Grok API key is not configured",
                model=target_model,
                hint="Configure credentials with 'support auth login grok' or set XAI_API_KEY.",
            )

        start_time = time.perf_counter()
        try:
            res = self._send_get("/models", timeout=5.0)
            latency_ms = (time.perf_counter() - start_time) * 1000.0

            if res.status_code == 401:
                return ProviderValidationResult(
                    provider=self.provider_type,
                    status=ProviderStatus.AUTH_FAILED,
                    message="xAI authentication rejected (HTTP 401)",
                    model=target_model,
                    latency_ms=latency_ms,
                    hint="Check that your XAI_API_KEY is valid and has not expired.",
                )

            if res.status_code != 200:
                return ProviderValidationResult(
                    provider=self.provider_type,
                    status=ProviderStatus.UNREACHABLE,
                    message=f"xAI API returned HTTP {res.status_code}",
                    model=target_model,
                    latency_ms=latency_ms,
                    hint="Check xAI service status or network connectivity.",
                )

            data = res.json()
            raw_models = data.get("data", [])
            available_ids = [m.get("id", "") for m in raw_models]

            if target_model and available_ids and target_model not in available_ids:
                return ProviderValidationResult(
                    provider=self.provider_type,
                    status=ProviderStatus.MODEL_UNAVAILABLE,
                    message=f"Model '{target_model}' not found in xAI account",
                    model=target_model,
                    latency_ms=latency_ms,
                    hint=f"Available xAI models: {', '.join(available_ids[:5])}",
                )

            return ProviderValidationResult(
                provider=self.provider_type,
                status=ProviderStatus.AVAILABLE,
                message="xAI Grok API connected and verified",
                model=target_model,
                latency_ms=latency_ms,
            )

        except (httpx.ConnectError, httpx.ConnectTimeout):
            return ProviderValidationResult(
                provider=self.provider_type,
                status=ProviderStatus.UNREACHABLE,
                message=f"Cannot reach xAI API at {self.base_url}",
                model=target_model,
                hint="Check your internet connection and DNS settings.",
            )
        except Exception as e:
            return ProviderValidationResult(
                provider=self.provider_type,
                status=ProviderStatus.UNREACHABLE,
                message=f"Error validating xAI provider: {e}",
                model=target_model,
                hint="Verify your network settings.",
            )

    def list_models(self) -> list[ModelInfo]:
        """Fetch available models from xAI /models endpoint."""
        if not self.api_key:
            # Fallback to known Grok models marked as unverified
            return [
                ModelInfo(
                    id="grok-2-latest",
                    name="grok-2-latest",
                    provider=self.provider_type,
                    description="xAI Grok 2 flagship reasoning model",
                    family="grok",
                ),
                ModelInfo(
                    id="grok-beta",
                    name="grok-beta",
                    provider=self.provider_type,
                    description="xAI Grok beta model",
                    family="grok",
                ),
            ]

        try:
            res = self._send_get("/models", timeout=5.0)
            if res.status_code != 200:
                return []

            data = res.json()
            models: list[ModelInfo] = []
            for m in data.get("data", []):
                model_id = m.get("id", "")
                models.append(
                    ModelInfo(
                        id=model_id,
                        name=model_id,
                        provider=self.provider_type,
                        family="grok",
                        details=m,
                    )
                )
            return models
        except Exception:
            return []

    def generate(
        self,
        prompt: str,
        model: Optional[str] = None,
        settings: Optional[GenerationSettings] = None,
    ) -> GenerationResponse:
        """Generate response via xAI /chat/completions."""
        if not self.api_key:
            raise ProviderNotConfiguredError(
                self.provider_type,
                "API key is not configured. Run 'support auth login grok' or set XAI_API_KEY.",
            )

        target_model = model or self.default_model
        st = settings or GenerationSettings()
        timeout = st.timeout

        payload = {
            "model": target_model,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": st.temperature,
        }
        if st.max_tokens is not None:
            payload["max_tokens"] = st.max_tokens
        if st.top_p is not None:
            payload["top_p"] = st.top_p
        if st.stop:
            payload["stop"] = st.stop

        start_time = time.perf_counter()
        try:
            res = self._send_post("/chat/completions", payload, timeout=timeout)
            elapsed_ms = (time.perf_counter() - start_time) * 1000.0

            if res.status_code == 401:
                raise LLMAuthenticationError(
                    self.provider_type,
                    "Authentication rejected (HTTP 401). Verify your XAI_API_KEY.",
                )

            if res.status_code == 404:
                raise ModelNotFoundError(
                    self.provider_type,
                    target_model,
                    hint=f"Model '{target_model}' not found in xAI API.",
                )

            if res.status_code != 200:
                raise GenerationError(
                    self.provider_type,
                    target_model,
                    f"xAI API returned HTTP {res.status_code}: {res.text}",
                )

            data = res.json()
            choices = data.get("choices", [])
            text = ""
            finish_reason = None
            if choices:
                first_choice = choices[0]
                text = first_choice.get("message", {}).get("content", "")
                finish_reason = first_choice.get("finish_reason")

            raw_usage = data.get("usage", {})
            usage = GenerationUsage(
                prompt_tokens=raw_usage.get("prompt_tokens"),
                completion_tokens=raw_usage.get("completion_tokens"),
                total_tokens=raw_usage.get("total_tokens"),
            )

            return GenerationResponse(
                text=text,
                provider=self.provider_type,
                model=target_model,
                usage=usage,
                latency_ms=elapsed_ms,
                finish_reason=finish_reason,
                raw_response=data,
            )

        except (httpx.ConnectError, httpx.ConnectTimeout):
            raise ProviderUnreachableError(
                self.provider_type,
                f"Cannot connect to xAI API at {self.base_url}",
                hint="Check internet connection and firewall settings.",
            )
        except httpx.TimeoutException:
            raise GenerationError(
                self.provider_type,
                target_model,
                f"xAI request timed out after {timeout:.1f}s",
            )
        except (ProviderNotConfiguredError, LLMAuthenticationError, ModelNotFoundError, GenerationError, ProviderUnreachableError):
            raise
        except Exception as e:
            raise GenerationError(
                self.provider_type,
                target_model,
                f"Unexpected failure communicating with xAI: {e}",
            )
