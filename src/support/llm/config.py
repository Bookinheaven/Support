"""LLM configuration management."""

import json
import os
from pathlib import Path
from typing import Optional
from pydantic import BaseModel, Field

from support.llm.models import LLMProviderType


class LLMConfig(BaseModel):
    """Persistent configuration for LLM providers and generation defaults."""

    default_provider: str = "ollama"
    default_model: str = "qwen2.5-coder:7b"
    ollama_base_url: str = "http://localhost:11434"
    temperature: float = 0.1
    timeout: float = 30.0
    provider_models: dict[str, str] = Field(
        default_factory=lambda: {
            "ollama": "qwen2.5-coder:7b",
            "grok": "grok-2-latest",
            "gemini": "gemini-2.0-flash",
        }
    )

    def get_model_for_provider(self, provider: str) -> str:
        """Get the configured model for a specific provider."""
        norm = provider.strip().lower()
        if norm in self.provider_models:
            return self.provider_models[norm]
        if norm == "ollama":
            return "qwen2.5-coder:7b"
        if norm == "grok":
            return "grok-2-latest"
        if norm == "gemini":
            return "gemini-2.0-flash"
        return self.default_model


class LLMConfigManager:
    """Manages reading and writing LLM configuration to ~/.support/llm_config.json."""

    def __init__(self, config_dir: Optional[Path] = None) -> None:
        if config_dir is not None:
            self.config_dir = Path(config_dir)
        else:
            env_dir = os.getenv("SUPPORT_CONFIG_DIR")
            if env_dir:
                self.config_dir = Path(env_dir)
            else:
                self.config_dir = Path.home() / ".support"

        self.config_path = self.config_dir / "llm_config.json"

    def load_config(self) -> LLMConfig:
        """Load configuration from disk or create default."""
        if not self.config_path.exists():
            default_config = LLMConfig()
            self.save_config(default_config)
            return default_config

        try:
            data = json.loads(self.config_path.read_text(encoding="utf-8"))
            return LLMConfig(**data)
        except Exception:
            return LLMConfig()

    def save_config(self, config: LLMConfig) -> None:
        """Save configuration to disk."""
        self.config_dir.mkdir(parents=True, exist_ok=True)
        self.config_path.write_text(
            json.dumps(config.model_dump(), indent=2),
            encoding="utf-8",
        )

    def set_active_provider_and_model(
        self, provider: str, model: Optional[str] = None
    ) -> LLMConfig:
        """Set the active provider and optionally active model."""
        config = self.load_config()
        prov_enum = LLMProviderType.from_string(provider)
        config.default_provider = prov_enum.value

        if model:
            config.provider_models[prov_enum.value] = model
            config.default_model = model
        else:
            config.default_model = config.get_model_for_provider(prov_enum.value)

        self.save_config(config)
        return config
