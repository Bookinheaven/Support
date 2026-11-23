"""Tests for persistent application configuration and model settings."""

import json
from pathlib import Path
import pytest

from support.infrastructure.config import (
    AppConfig,
    DEFAULT_CONFIG_FILENAME,
    DEFAULT_OLLAMA_HOST,
    get_assistant_model,
    get_config_dir,
    get_config_path,
    get_database_path,
    get_ollama_host,
    load_config,
    save_config,
)


def test_app_config_defaults() -> None:
    cfg = AppConfig()
    assert cfg.models == {}
    assert cfg.ollama_host == DEFAULT_OLLAMA_HOST
    assert cfg.get_model("assistant") is None


def test_app_config_set_and_unset() -> None:
    cfg = AppConfig()
    cfg.set_model("assistant", "qwen2.5-coder:7b")
    assert cfg.get_model("assistant") == "qwen2.5-coder:7b"

    # Unset existing
    assert cfg.unset_model("assistant") is True
    assert cfg.get_model("assistant") is None

    # Unset nonexistent
    assert cfg.unset_model("assistant") is False


def test_app_config_to_dict_and_from_dict() -> None:
    cfg = AppConfig(models={"assistant": "qwen2.5-coder:3b"}, ollama_host="http://custom:11434")
    data = cfg.to_dict()
    assert data == {
        "models": {"assistant": "qwen2.5-coder:3b"},
        "ollama_host": "http://custom:11434",
    }

    reloaded = AppConfig.from_dict(data)
    assert reloaded.get_model("assistant") == "qwen2.5-coder:3b"
    assert reloaded.ollama_host == "http://custom:11434"


def test_app_config_from_dict_malformed_inputs() -> None:
    # Non-dict inputs
    assert AppConfig.from_dict(None).models == {}
    assert AppConfig.from_dict("invalid").models == {}
    assert AppConfig.from_dict([]).models == {}

    # Malformed models field
    cfg = AppConfig.from_dict({"models": "not-a-dict", "ollama_host": 123})
    assert cfg.models == {}
    assert cfg.ollama_host == DEFAULT_OLLAMA_HOST

    # Empty string entries filtered out
    cfg2 = AppConfig.from_dict({"models": {"assistant": "  ", "other": 123, "valid": "model:1"}})
    assert cfg2.models == {"valid": "model:1"}


def test_save_and_load_config_roundtrip(tmp_path: Path) -> None:
    config_file = tmp_path / DEFAULT_CONFIG_FILENAME
    cfg = AppConfig(models={"assistant": "qwen2.5-coder:14b"}, ollama_host="http://localhost:11434")

    save_config(cfg, config_file)
    assert config_file.is_file()

    loaded = load_config(config_file)
    assert loaded.get_model("assistant") == "qwen2.5-coder:14b"
    assert loaded.ollama_host == "http://localhost:11434"


def test_load_config_missing_file_returns_defaults(tmp_path: Path) -> None:
    config_file = tmp_path / "nonexistent.json"
    cfg = load_config(config_file)
    assert cfg.models == {}
    assert cfg.ollama_host == DEFAULT_OLLAMA_HOST


def test_load_config_corrupted_file_returns_defaults(tmp_path: Path) -> None:
    config_file = tmp_path / "corrupted.json"
    config_file.write_text("{invalid-json-content", encoding="utf-8")

    cfg = load_config(config_file)
    assert cfg.models == {}
    assert cfg.ollama_host == DEFAULT_OLLAMA_HOST


def test_get_config_path_resolution(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # Explicit path
    explicit = tmp_path / "custom" / "cfg.json"
    assert get_config_path(explicit) == explicit

    # Explicit directory
    assert get_config_path(tmp_path) == tmp_path / DEFAULT_CONFIG_FILENAME

    # Environment variable SUPPORT_HOME
    monkeypatch.setenv("SUPPORT_HOME", str(tmp_path))
    assert get_config_dir() == tmp_path
    assert get_config_path() == tmp_path / DEFAULT_CONFIG_FILENAME


def test_get_assistant_model_precedence(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config_file = tmp_path / DEFAULT_CONFIG_FILENAME

    # 1. Nothing configured
    monkeypatch.delenv("SUPPORT_MODEL", raising=False)
    monkeypatch.delenv("OLLAMA_MODEL", raising=False)
    assert get_assistant_model(config_file) is None

    # 2. Persisted in config.json
    cfg = AppConfig(models={"assistant": "saved-model:7b"})
    save_config(cfg, config_file)
    assert get_assistant_model(config_file) == "saved-model:7b"

    # 3. Environment override takes precedence when set
    monkeypatch.setenv("SUPPORT_MODEL", "env-model:14b")
    assert get_assistant_model(config_file) == "env-model:14b"

    # 4. Fallback OLLAMA_MODEL
    monkeypatch.delenv("SUPPORT_MODEL")
    monkeypatch.setenv("OLLAMA_MODEL", "fallback-ollama:3b")
    assert get_assistant_model(config_file) == "fallback-ollama:3b"


def test_get_ollama_host_precedence(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config_file = tmp_path / DEFAULT_CONFIG_FILENAME

    # 1. Default
    monkeypatch.delenv("SUPPORT_OLLAMA_HOST", raising=False)
    monkeypatch.delenv("OLLAMA_HOST", raising=False)
    assert get_ollama_host(config_file) == DEFAULT_OLLAMA_HOST

    # 2. Persisted in config.json
    cfg = AppConfig(ollama_host="http://192.168.1.100:11434/")
    save_config(cfg, config_file)
    assert get_ollama_host(config_file) == "http://192.168.1.100:11434"

    # 3. Environment override takes precedence
    monkeypatch.setenv("SUPPORT_OLLAMA_HOST", "http://env-host:11434/")
    assert get_ollama_host(config_file) == "http://env-host:11434"
