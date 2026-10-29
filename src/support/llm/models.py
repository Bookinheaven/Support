"""Data models for LLM providers, models, and generation."""

from enum import Enum
from typing import Any, Optional
from pydantic import BaseModel, Field, ConfigDict


class LLMProviderType(str, Enum):
    """Supported LLM provider types."""

    OLLAMA = "ollama"
    GROK = "grok"
    GEMINI = "gemini"

    @classmethod
    def from_string(cls, value: str) -> "LLMProviderType":
        """Resolve string into provider enum."""
        norm = value.strip().lower()
        if norm in ("ollama", "local"):
            return cls.OLLAMA
        if norm in ("grok", "xai"):
            return cls.GROK
        if norm in ("gemini", "google"):
            return cls.GEMINI
        raise ValueError(
            f"Unsupported LLM provider '{value}'. Supported providers are: ollama, grok, gemini."
        )


class ProviderStatus(str, Enum):
    """Health/readiness status of an LLM provider."""

    AVAILABLE = "AVAILABLE"
    NOT_CONFIGURED = "NOT CONFIGURED"
    UNREACHABLE = "UNREACHABLE"
    MODEL_UNAVAILABLE = "MODEL UNAVAILABLE"
    AUTH_FAILED = "AUTH FAILED"


class ModelInfo(BaseModel):
    """Metadata representing an LLM model."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    id: str
    name: str
    provider: str
    description: Optional[str] = None
    size_bytes: Optional[int] = None
    family: Optional[str] = None
    is_default: bool = False
    details: dict[str, Any] = Field(default_factory=dict)

    @property
    def display_size(self) -> str:
        """Format size in MB/GB."""
        if self.size_bytes is None:
            return "-"
        gb = self.size_bytes / (1024**3)
        if gb >= 1.0:
            return f"{gb:.1f} GB"
        mb = self.size_bytes / (1024**2)
        return f"{mb:.0f} MB"


class GenerationSettings(BaseModel):
    """Hyperparameters and runtime settings for prompt generation."""

    temperature: float = Field(default=0.1, ge=0.0, le=2.0)
    max_tokens: Optional[int] = Field(default=None, gt=0)
    timeout: float = Field(default=30.0, gt=0.0)
    top_p: Optional[float] = Field(default=None, ge=0.0, le=1.0)
    stop: list[str] = Field(default_factory=list)


class GenerationUsage(BaseModel):
    """Token usage metrics."""

    prompt_tokens: Optional[int] = None
    completion_tokens: Optional[int] = None
    total_tokens: Optional[int] = None

    @property
    def display_summary(self) -> str:
        """Formatted summary of token consumption."""
        parts = []
        if self.prompt_tokens is not None:
            parts.append(f"{self.prompt_tokens} in")
        if self.completion_tokens is not None:
            parts.append(f"{self.completion_tokens} out")
        if self.total_tokens is not None and not parts:
            parts.append(f"{self.total_tokens} tokens")
        return ", ".join(parts) if parts else "-"


class GenerationResponse(BaseModel):
    """Normalized response from any LLM provider."""

    text: str
    provider: str
    model: str
    usage: Optional[GenerationUsage] = None
    latency_ms: Optional[float] = None
    finish_reason: Optional[str] = None
    raw_response: Optional[dict[str, Any]] = None


class ProviderValidationResult(BaseModel):
    """Result of validating provider connectivity, authentication, and model availability."""

    provider: str
    status: ProviderStatus
    message: str
    model: Optional[str] = None
    latency_ms: Optional[float] = None
    hint: Optional[str] = None
