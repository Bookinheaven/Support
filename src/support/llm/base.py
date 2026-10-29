"""Abstract base interface for LLM provider adapters."""

from abc import ABC, abstractmethod
from typing import Optional

from support.llm.models import (
    GenerationResponse,
    GenerationSettings,
    ModelInfo,
    ProviderValidationResult,
)


class BaseLLMProvider(ABC):
    """Common interface for all LLM provider adapters."""

    @property
    @abstractmethod
    def provider_type(self) -> str:
        """The identifier string of this provider (e.g. 'ollama', 'grok', 'gemini')."""
        ...

    @abstractmethod
    def validate_configuration(
        self, model: Optional[str] = None
    ) -> ProviderValidationResult:
        """Validate credentials, network reachability, and model availability."""
        ...

    @abstractmethod
    def list_models(self) -> list[ModelInfo]:
        """Discover available or installed models from the provider."""
        ...

    @abstractmethod
    def generate(
        self,
        prompt: str,
        model: Optional[str] = None,
        settings: Optional[GenerationSettings] = None,
    ) -> GenerationResponse:
        """Generate text from a prompt using the specified or default model."""
        ...
