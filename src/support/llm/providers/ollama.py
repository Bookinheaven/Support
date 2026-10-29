"""Ollama local LLM provider adapter."""

import os
import time
from typing import Optional
import httpx

from support.core.exceptions import (
    GenerationError,
    ModelNotFoundError,
    ProviderUnreachableError,
)
from support.llm.base import BaseLLMProvider
from support.llm.models import (
    GenerationResponse,
    GenerationSettings,
    GenerationUsage,
    ModelInfo,
    ProviderStatus,
    ProviderValidationResult,
)


class OllamaProvider(BaseLLMProvider):
    """Adapter for local Ollama instances."""

    def __init__(
        self,
        base_url: Optional[str] = None,
        default_model: str = "qwen2.5-coder:7b",
        client: Optional[httpx.Client] = None,
    ) -> None:
        self.base_url = (
            base_url
            or os.getenv("OLLAMA_HOST")
            or "http://localhost:11434"
        ).rstrip("/")
        self.default_model = default_model
        self._client = client

    @property
    def provider_type(self) -> str:
        return "ollama"

    def _get_client(self, timeout: float = 10.0) -> httpx.Client:
        if self._client is not None:
            return self._client
        return httpx.Client(base_url=self.base_url, timeout=timeout)

    def validate_configuration(
        self, model: Optional[str] = None
    ) -> ProviderValidationResult:
        """Verify that the local Ollama instance is online and model is available."""
        target_model = model or self.default_model
        start_time = time.perf_counter()

        try:
            with self._get_client(timeout=3.0) as client:
                res = client.get("/api/tags")
                latency_ms = (time.perf_counter() - start_time) * 1000.0

                if res.status_code != 200:
                    return ProviderValidationResult(
                        provider=self.provider_type,
                        status=ProviderStatus.UNREACHABLE,
                        message=f"Ollama returned HTTP {res.status_code}",
                        model=target_model,
                        latency_ms=latency_ms,
                        hint="Check Ollama logs to ensure server health.",
                    )

                data = res.json()
                raw_models = data.get("models", [])
                installed_names = [m.get("name", "") for m in raw_models]

                # Check if target model is installed (allow matching with or without tag)
                def matches(cand: str, target: str) -> bool:
                    if cand.lower() == target.lower():
                        return True
                    if target.endswith(":latest") and cand.lower() == target[:-7].lower():
                        return True
                    if cand.endswith(":latest") and cand[:-7].lower() == target.lower():
                        return True
                    return False

                if target_model and not any(matches(name, target_model) for name in installed_names):
                    return ProviderValidationResult(
                        provider=self.provider_type,
                        status=ProviderStatus.MODEL_UNAVAILABLE,
                        message=f"Model '{target_model}' is not installed in Ollama",
                        model=target_model,
                        latency_ms=latency_ms,
                        hint=f"Install it by running: ollama pull {target_model}",
                    )

                count = len(installed_names)
                return ProviderValidationResult(
                    provider=self.provider_type,
                    status=ProviderStatus.AVAILABLE,
                    message=f"Ollama running · {count} model(s) available",
                    model=target_model,
                    latency_ms=latency_ms,
                )

        except (httpx.ConnectError, httpx.ConnectTimeout):
            return ProviderValidationResult(
                provider=self.provider_type,
                status=ProviderStatus.UNREACHABLE,
                message=f"Ollama is not running at {self.base_url}",
                model=target_model,
                hint="Start the Ollama application or run 'ollama serve' in a terminal.",
            )
        except Exception as e:
            return ProviderValidationResult(
                provider=self.provider_type,
                status=ProviderStatus.UNREACHABLE,
                message=f"Failed to connect to Ollama: {e}",
                model=target_model,
                hint="Verify Ollama host setting and local network connectivity.",
            )

    def list_models(self) -> list[ModelInfo]:
        """Query installed models from Ollama /api/tags."""
        try:
            with self._get_client(timeout=5.0) as client:
                res = client.get("/api/tags")
                if res.status_code != 200:
                    return []

                data = res.json()
                models: list[ModelInfo] = []
                for m in data.get("models", []):
                    name = m.get("name", "")
                    details = m.get("details", {})
                    models.append(
                        ModelInfo(
                            id=name,
                            name=name,
                            provider=self.provider_type,
                            size_bytes=m.get("size"),
                            family=details.get("family"),
                            details=details,
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
        """Generate a response using Ollama /api/generate."""
        target_model = model or self.default_model
        st = settings or GenerationSettings()
        timeout = st.timeout

        options: dict[str, object] = {
            "temperature": st.temperature,
        }
        if st.top_p is not None:
            options["top_p"] = st.top_p
        if st.max_tokens is not None:
            options["num_predict"] = st.max_tokens
        if st.stop:
            options["stop"] = st.stop

        payload = {
            "model": target_model,
            "prompt": prompt,
            "stream": False,
            "options": options,
        }

        start_time = time.perf_counter()
        try:
            with self._get_client(timeout=timeout) as client:
                res = client.post("/api/generate", json=payload)
                elapsed_ms = (time.perf_counter() - start_time) * 1000.0

                if res.status_code == 404:
                    raise ModelNotFoundError(
                        self.provider_type,
                        target_model,
                        hint=f"Model '{target_model}' is not installed in Ollama. Pull it with: ollama pull {target_model}",
                    )

                if res.status_code != 200:
                    raise GenerationError(
                        self.provider_type,
                        target_model,
                        f"Ollama API returned HTTP {res.status_code}: {res.text}",
                    )

                data = res.json()
                text = data.get("response", "")
                p_tokens = data.get("prompt_eval_count")
                c_tokens = data.get("eval_count")
                total_tokens = (
                    (p_tokens or 0) + (c_tokens or 0)
                    if (p_tokens is not None or c_tokens is not None)
                    else None
                )

                usage = GenerationUsage(
                    prompt_tokens=p_tokens,
                    completion_tokens=c_tokens,
                    total_tokens=total_tokens,
                )

                # Ollama total_duration is in nanoseconds
                total_duration_ns = data.get("total_duration")
                if total_duration_ns:
                    elapsed_ms = total_duration_ns / 1_000_000.0

                return GenerationResponse(
                    text=text,
                    provider=self.provider_type,
                    model=target_model,
                    usage=usage,
                    latency_ms=elapsed_ms,
                    finish_reason=data.get("done_reason"),
                    raw_response=data,
                )

        except (httpx.ConnectError, httpx.ConnectTimeout):
            raise ProviderUnreachableError(
                self.provider_type,
                f"Cannot connect to Ollama at {self.base_url}",
                hint="Start Ollama before issuing generation requests.",
            )
        except httpx.TimeoutException:
            raise GenerationError(
                self.provider_type,
                target_model,
                f"Generation request timed out after {timeout:.1f}s",
                details="Consider lowering max_tokens, using a smaller model, or increasing the timeout.",
            )
        except (ModelNotFoundError, ProviderUnreachableError, GenerationError):
            raise
        except Exception as e:
            raise GenerationError(
                self.provider_type,
                target_model,
                f"Unexpected generation failure: {e}",
            )
