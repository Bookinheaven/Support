"""Configuration path resolution and persistent application configuration for Support."""

from __future__ import annotations

from dataclasses import dataclass, field
import json
import os
from pathlib import Path
import tempfile
from typing import Any
import platformdirs

DEFAULT_DATABASE_FILENAME = "profiles.sqlite3"
DEFAULT_CONFIG_FILENAME = "config.json"
SUPPORT_HOME_ENV_VAR = "SUPPORT_HOME"
SUPPORT_MODEL_ENV_VAR = "SUPPORT_MODEL"
OLLAMA_MODEL_ENV_VAR = "OLLAMA_MODEL"
SUPPORT_OLLAMA_HOST_ENV_VAR = "SUPPORT_OLLAMA_HOST"
OLLAMA_HOST_ENV_VAR = "OLLAMA_HOST"
DEFAULT_OLLAMA_HOST = "http://localhost:11434"


@dataclass
class AppConfig:
    """User-level persistent application configuration."""

    models: dict[str, str] = field(default_factory=dict)
    ollama_host: str = DEFAULT_OLLAMA_HOST

    def get_model(self, role: str = "assistant") -> str | None:
        """Retrieve configured model name for a role (e.g. 'assistant')."""
        val = self.models.get(role)
        return val.strip() if val and val.strip() else None

    def set_model(self, role: str, model_name: str) -> None:
        """Assign model name to a role."""
        self.models[role] = model_name.strip()

    def unset_model(self, role: str) -> bool:
        """Remove configured model for a role. Returns True if removed."""
        if role in self.models:
            del self.models[role]
            return True
        return False

    def to_dict(self) -> dict[str, Any]:
        """Convert configuration to dictionary."""
        return {
            "models": dict(self.models),
            "ollama_host": self.ollama_host,
        }

    @classmethod
    def from_dict(cls, data: Any) -> AppConfig:
        """Construct AppConfig from dictionary with validation."""
        if not isinstance(data, dict):
            return cls()
        models = data.get("models")
        valid_models: dict[str, str] = {}
        if isinstance(models, dict):
            for k, v in models.items():
                if isinstance(k, str) and isinstance(v, str) and v.strip():
                    valid_models[k.strip()] = v.strip()
        host = data.get("ollama_host")
        valid_host = host.strip() if isinstance(host, str) and host.strip() else DEFAULT_OLLAMA_HOST
        return cls(models=valid_models, ollama_host=valid_host)


def get_config_dir() -> Path:
    """Resolve the user-level configuration directory for Support."""
    support_home = os.environ.get(SUPPORT_HOME_ENV_VAR)
    if support_home:
        return Path(support_home)
    return platformdirs.user_config_path("support", appauthor=False)


def get_config_path(override_path: Path | str | None = None) -> Path:
    """Resolve the file path to config.json."""
    if override_path is not None:
        p = Path(override_path)
        if p.is_dir():
            return p / DEFAULT_CONFIG_FILENAME
        return p
    return get_config_dir() / DEFAULT_CONFIG_FILENAME


def load_config(config_path: Path | str | None = None) -> AppConfig:
    """Load persistent configuration from disk. Gracefully handles missing or malformed files."""
    path = get_config_path(config_path)
    if not path.is_file():
        return AppConfig()
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
            return AppConfig.from_dict(data)
    except Exception:
        return AppConfig()


def save_config(config: AppConfig, config_path: Path | str | None = None) -> None:
    """Persist configuration to disk using atomic replacement."""
    path = get_config_path(config_path)
    path.parent.mkdir(parents=True, exist_ok=True)

    payload = json.dumps(config.to_dict(), indent=2) + "\n"
    tmp_path = None
    try:
        with tempfile.NamedTemporaryFile("w", dir=path.parent, delete=False, encoding="utf-8") as tf:
            tmp_path = Path(tf.name)
            tf.write(payload)
        tmp_path.replace(path)
    except Exception:
        if tmp_path and tmp_path.exists():
            try:
                tmp_path.unlink()
            except Exception:
                pass
        raise


def get_assistant_model(config_path: Path | str | None = None) -> str | None:
    """Resolve configured assistant model with persisted configuration precedence.

    Environment variables (SUPPORT_MODEL / OLLAMA_MODEL) serve only as an advanced override.
    """
    env_model = os.environ.get(SUPPORT_MODEL_ENV_VAR) or os.environ.get(OLLAMA_MODEL_ENV_VAR)
    if env_model and env_model.strip():
        return env_model.strip()

    cfg = load_config(config_path)
    return cfg.get_model("assistant")


def get_ollama_host(config_path: Path | str | None = None) -> str:
    """Resolve configured Ollama server host URL."""
    env_host = os.environ.get(SUPPORT_OLLAMA_HOST_ENV_VAR) or os.environ.get(OLLAMA_HOST_ENV_VAR)
    if env_host and env_host.strip():
        return env_host.strip().rstrip("/")

    cfg = load_config(config_path)
    return cfg.ollama_host.strip().rstrip("/")


def get_database_path(override_path: Path | str | None = None) -> Path:
    """Resolve the SQLite database path for profiles.

    Resolution order:
    1. Explicit override_path (if provided).
    2. SUPPORT_HOME environment variable (as the root directory for profiles.sqlite3).
    3. User configuration directory via platformdirs.
    """
    if override_path is not None:
        path = Path(override_path)
        if path.is_dir():
            return path / DEFAULT_DATABASE_FILENAME
        return path

    return get_config_dir() / DEFAULT_DATABASE_FILENAME
