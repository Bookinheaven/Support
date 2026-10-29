"""Google Gemini LLM provider adapter."""

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


class GeminiProvider(BaseLLMProvider):
    """Adapter for Google Gemini REST API."""

    def __init__(
        self,
        api_key: Optional[str] = None,
        base_url: Optional[str] = None,
        default_model: str = "gemini-2.0-flash",
        client: Optional[httpx.Client] = None,
    ) -> None:
        self.api_key = api_key or CredentialManager.get_api_key("gemini")
        self.base_url = (
            base_url
            or os.getenv("GEMINI_BASE_URL")
            or "https://generativelanguage.googleapis.com/v1beta"
        ).rstrip("/")
        self.default_model = default_model
        self._client = client

    @property
    def provider_type(self) -> str:
        return "gemini"

    def _get_headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            # Send API key via standard header to keep URLs clean and avoid secret leakage
            headers["x-goog-api-key"] = self.api_key
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

    def _clean_model_name(self, model: str) -> str:
        """Strip 'models/' prefix if present."""
        if model.startswith("models/"):
            return model[7:]
        return model

    def validate_configuration(
        self, model: Optional[str] = None
    ) -> ProviderValidationResult:
        """Validate Gemini API key and model availability."""
        raw_target = model or self.default_model
        target_model = self._clean_model_name(raw_target)

        if not self.api_key:
            return ProviderValidationResult(
                provider=self.provider_type,
                status=ProviderStatus.NOT_CONFIGURED,
                message="Gemini API key is not configured",
                model=target_model,
                hint="Configure credentials with 'support auth login gemini' or set GEMINI_API_KEY.",
            )

        start_time = time.perf_counter()
        try:
            res = self._send_get("/models", timeout=5.0)
            latency_ms = (time.perf_counter() - start_time) * 1000.0

            if res.status_code in (400, 403):
                return ProviderValidationResult(
                    provider=self.provider_type,
                    status=ProviderStatus.AUTH_FAILED,
                    message=f"Google Gemini authentication failed (HTTP {res.status_code})",
                    model=target_model,
                    latency_ms=latency_ms,
                    hint="Check that your GEMINI_API_KEY is valid in Google AI Studio.",
                )

            if res.status_code != 200:
                return ProviderValidationResult(
                    provider=self.provider_type,
                    status=ProviderStatus.UNREACHABLE,
                    message=f"Gemini API returned HTTP {res.status_code}",
                    model=target_model,
                    latency_ms=latency_ms,
                    hint="Check Gemini service status or network connectivity.",
                )

            data = res.json()
            raw_models = data.get("models", [])
            available_ids = [
                self._clean_model_name(m.get("name", "")) for m in raw_models
            ]

            if target_model and available_ids and target_model not in available_ids:
                return ProviderValidationResult(
                    provider=self.provider_type,
                    status=ProviderStatus.MODEL_UNAVAILABLE,
                    message=f"Model '{target_model}' not found in Gemini API",
                    model=target_model,
                    latency_ms=latency_ms,
                    hint=f"Available Gemini models: {', '.join(available_ids[:5])}",
                )

            return ProviderValidationResult(
                provider=self.provider_type,
                status=ProviderStatus.AVAILABLE,
                message="Google Gemini API connected and verified",
                model=target_model,
                latency_ms=latency_ms,
            )

        except (httpx.ConnectError, httpx.ConnectTimeout):
            return ProviderValidationResult(
                provider=self.provider_type,
                status=ProviderStatus.UNREACHABLE,
                message=f"Cannot reach Gemini API at {self.base_url}",
                model=target_model,
                hint="Check your internet connection and DNS settings.",
            )
        except Exception as e:
            return ProviderValidationResult(
                provider=self.provider_type,
                status=ProviderStatus.UNREACHABLE,
                message=f"Error validating Gemini provider: {e}",
                model=target_model,
                hint="Verify your network settings.",
            )

    def list_models(self) -> list[ModelInfo]:
        """Fetch available models from Gemini /models endpoint."""
        if not self.api_key:
            return [
                ModelInfo(
                    id="gemini-2.0-flash",
                    name="gemini-2.0-flash",
                    provider=self.provider_type,
                    description="Google Gemini 2.0 Flash generation model",
                    family="gemini",
                ),
                ModelInfo(
                    id="gemini-1.5-flash",
                    name="gemini-1.5-flash",
                    provider=self.provider_type,
                    description="Google Gemini 1.5 Flash lightweight model",
                    family="gemini",
                ),
                ModelInfo(
                    id="gemini-1.5-pro",
                    name="gemini-1.5-pro",
                    provider=self.provider_type,
                    description="Google Gemini 1.5 Pro complex reasoning model",
                    family="gemini",
                ),
            ]

        try:
            res = self._send_get("/models", timeout=5.0)
            if res.status_code != 200:
                return []

            data = res.json()
            models: list[ModelInfo] = []
            for m in data.get("models", []):
                # Filter for models supporting generateContent
                methods = m.get("supportedGenerationMethods", [])
                if "generateContent" not in methods:
                    continue

                raw_name = m.get("name", "")
                clean_id = self._clean_model_name(raw_name)
                models.append(
                    ModelInfo(
                        id=clean_id,
                        name=clean_id,
                        provider=self.provider_type,
                        description=m.get("description"),
                        family="gemini",
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
        """Generate response via Gemini generateContent."""
        if not self.api_key:
            raise ProviderNotConfiguredError(
                self.provider_type,
                "API key is not configured. Run 'support auth login gemini' or set GEMINI_API_KEY.",
            )

        raw_target = model or self.default_model
        target_model = self._clean_model_name(raw_target)
        st = settings or GenerationSettings()
        timeout = st.timeout

        gen_config: dict[str, object] = {
            "temperature": st.temperature,
        }
        if st.max_tokens is not None:
            gen_config["maxOutputTokens"] = st.max_tokens
        if st.top_p is not None:
            gen_config["topP"] = st.top_p
        if st.stop:
            gen_config["stopSequences"] = st.stop

        payload = {
            "contents": [
                {
                    "parts": [{"text": prompt}]
                }
            ],
            "generationConfig": gen_config,
        }

        endpoint = f"/models/{target_model}:generateContent"

        start_time = time.perf_counter()
        try:
            res = self._send_post(endpoint, payload, timeout=timeout)
            elapsed_ms = (time.perf_counter() - start_time) * 1000.0

            if res.status_code in (400, 403):
                # Check if error is related to API key or quota
                err_body = res.json().get("error", {})
                err_msg = err_body.get("message", res.text)
                if "API key" in err_msg or "PERMISSION_DENIED" in err_msg:
                    raise LLMAuthenticationError(
                        self.provider_type,
                        f"Authentication failed: {err_msg}",
                    )
                raise GenerationError(
                    self.provider_type,
                    target_model,
                    f"Gemini API returned HTTP {res.status_code}: {err_msg}",
                )

            if res.status_code == 404:
                raise ModelNotFoundError(
                    self.provider_type,
                    target_model,
                    hint=f"Model '{target_model}' not found in Google Gemini API.",
                )

            if res.status_code != 200:
                raise GenerationError(
                    self.provider_type,
                    target_model,
                    f"Gemini API returned HTTP {res.status_code}: {res.text}",
                )

            data = res.json()
            candidates = data.get("candidates", [])
            text = ""
            finish_reason = None

            if candidates:
                cand = candidates[0]
                finish_reason = cand.get("finishReason")
                parts = cand.get("content", {}).get("parts", [])
                if parts:
                    text = parts[0].get("text", "")

            meta = data.get("usageMetadata", {})
            usage = GenerationUsage(
                prompt_tokens=meta.get("promptTokenCount"),
                completion_tokens=meta.get("candidatesTokenCount"),
                total_tokens=meta.get("totalTokenCount"),
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
                f"Cannot connect to Gemini API at {self.base_url}",
                hint="Check internet connection and firewall settings.",
            )
        except httpx.TimeoutException:
            raise GenerationError(
                self.provider_type,
                target_model,
                f"Gemini request timed out after {timeout:.1f}s",
            )
        except (ProviderNotConfiguredError, LLMAuthenticationError, ModelNotFoundError, GenerationError, ProviderUnreachableError):
            raise
        except Exception as e:
            raise GenerationError(
                self.provider_type,
                target_model,
                f"Unexpected failure communicating with Google Gemini: {e}",
            )
