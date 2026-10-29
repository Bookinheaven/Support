"""LLM Gateway application service."""

from typing import Optional

from support.llm.base import BaseLLMProvider
from support.llm.config import LLMConfig, LLMConfigManager
from support.llm.models import (
    GenerationResponse,
    GenerationSettings,
    LLMProviderType,
    ModelInfo,
    ProviderValidationResult,
)
from support.llm.providers.gemini import GeminiProvider
from support.llm.providers.grok import GrokProvider
from support.llm.providers.ollama import OllamaProvider


class LLMService:
    """Unified application gateway for LLM interactions across Ollama, Grok, and Gemini."""

    def __init__(
        self,
        config_manager: Optional[LLMConfigManager] = None,
        custom_providers: Optional[dict[str, BaseLLMProvider]] = None,
    ) -> None:
        self.config_manager = config_manager or LLMConfigManager()
        self._custom_providers = custom_providers or {}

    def get_config(self) -> LLMConfig:
        """Load the current persistent LLM configuration."""
        return self.config_manager.load_config()

    def get_provider(self, provider_name: Optional[str] = None) -> BaseLLMProvider:
        """Instantiate and return the requested or default provider adapter."""
        config = self.get_config()
        raw_name = provider_name or config.default_provider
        prov_enum = LLMProviderType.from_string(raw_name)
        key = prov_enum.value

        if key in self._custom_providers:
            return self._custom_providers[key]

        if prov_enum == LLMProviderType.OLLAMA:
            return OllamaProvider(
                base_url=config.ollama_base_url,
                default_model=config.get_model_for_provider("ollama"),
            )
        elif prov_enum == LLMProviderType.GROK:
            return GrokProvider(
                default_model=config.get_model_for_provider("grok"),
            )
        elif prov_enum == LLMProviderType.GEMINI:
            return GeminiProvider(
                default_model=config.get_model_for_provider("gemini"),
            )
        else:
            raise ValueError(f"Unknown provider type: {raw_name}")

    def generate(
        self,
        prompt: str,
        provider: Optional[str] = None,
        model: Optional[str] = None,
        settings: Optional[GenerationSettings] = None,
    ) -> GenerationResponse:
        """Generate a response using the selected or configured default provider and model.

        Callers can override the provider or model for this single call without mutating persistent configuration.
        """
        config = self.get_config()
        target_provider_name = (
            LLMProviderType.from_string(provider).value
            if provider
            else config.default_provider
        )
        provider_adapter = self.get_provider(target_provider_name)

        target_model = model or (
            config.default_model
            if target_provider_name == config.default_provider
            else config.get_model_for_provider(target_provider_name)
        )

        st = settings or GenerationSettings(
            temperature=config.temperature,
            timeout=config.timeout,
        )

        return provider_adapter.generate(prompt=prompt, model=target_model, settings=st)

    def list_models(self, provider: Optional[str] = None) -> dict[str, list[ModelInfo]]:
        """List available/installed models for a single provider or all providers."""
        config = self.get_config()
        results: dict[str, list[ModelInfo]] = {}

        if provider:
            target_prov = LLMProviderType.from_string(provider).value
            adapter = self.get_provider(target_prov)
            models = adapter.list_models()
            for m in models:
                if m.id == config.default_model and target_prov == config.default_provider:
                    m.is_default = True
            results[target_prov] = models
            return results

        # Query all three providers
        for p_type in (LLMProviderType.OLLAMA, LLMProviderType.GROK, LLMProviderType.GEMINI):
            adapter = self.get_provider(p_type.value)
            models = adapter.list_models()
            for m in models:
                if m.id == config.default_model and p_type.value == config.default_provider:
                    m.is_default = True
            results[p_type.value] = models

        return results

    def validate_provider(
        self, provider: Optional[str] = None, model: Optional[str] = None
    ) -> ProviderValidationResult:
        """Validate connectivity and model availability for a provider."""
        adapter = self.get_provider(provider)
        config = self.get_config()
        target_model = model or (
            config.default_model
            if (not provider or provider == config.default_provider)
            else config.get_model_for_provider(adapter.provider_type)
        )
        return adapter.validate_configuration(model=target_model)

    def validate_all(self) -> dict[str, ProviderValidationResult]:
        """Validate all supported providers."""
        results: dict[str, ProviderValidationResult] = {}
        for p_type in (LLMProviderType.OLLAMA, LLMProviderType.GROK, LLMProviderType.GEMINI):
            adapter = self.get_provider(p_type.value)
            results[p_type.value] = adapter.validate_configuration()
        return results

    def set_active_provider(
        self, provider: str, model: Optional[str] = None
    ) -> LLMConfig:
        """Change the active persistent provider and optionally model."""
        return self.config_manager.set_active_provider_and_model(provider, model)
