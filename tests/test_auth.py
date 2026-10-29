"""Tests for secure credential management and support auth CLI commands."""

import os
from unittest.mock import MagicMock, patch
import pytest
from typer.testing import CliRunner

from support.cli.main import app
from support.core.exceptions import KeyringUnavailableError
from support.llm.credentials import (
    CredentialManager,
    redact_api_key,
)
from support.llm.models import ProviderStatus, ProviderValidationResult
from support.llm.providers.gemini import GeminiProvider
from support.llm.providers.grok import GrokProvider

runner = CliRunner()


@pytest.fixture(autouse=True)
def clean_env():
    """Ensure cloud API keys are clean for each test unless explicitly set."""
    old_xai = os.environ.pop("XAI_API_KEY", None)
    old_gemini = os.environ.pop("GEMINI_API_KEY", None)
    yield
    if old_xai is not None:
        os.environ["XAI_API_KEY"] = old_xai
    if old_gemini is not None:
        os.environ["GEMINI_API_KEY"] = old_gemini


def test_redact_api_key():
    """Verify secrets are redacted safely."""
    assert redact_api_key(None) == ""
    assert redact_api_key("") == ""
    assert redact_api_key("12345") == "******"
    assert redact_api_key("secret") == "******"
    assert redact_api_key("xai-1234567890-abcdef") == "xai...def"
    assert redact_api_key("AIzaSyD-123456789") == "AIz...789"


def test_credential_manager_storage_and_retrieval():
    """Verify storage, retrieval, and deletion using mocked keyring."""
    mock_store = {}

    def mock_get_password(service, account):
        return mock_store.get(f"{service}:{account}")

    def mock_set_password(service, account, password):
        mock_store[f"{service}:{account}"] = password

    def mock_delete_password(service, account):
        mock_store.pop(f"{service}:{account}", None)

    with patch("keyring.get_password", side_effect=mock_get_password), \
         patch("keyring.set_password", side_effect=mock_set_password), \
         patch("keyring.delete_password", side_effect=mock_delete_password), \
         patch.object(CredentialManager, "is_keyring_available", return_value=True), \
         patch.object(CredentialManager, "get_backend_name", return_value="MockKeyring"):

        # Initially unconfigured
        assert CredentialManager.get_api_key("grok") is None
        is_conf, src = CredentialManager.get_credential_source("grok")
        assert not is_conf
        assert src == "Not configured"

        # Save key
        CredentialManager.set_api_key("grok", "xai-test-key-123")
        assert CredentialManager.get_api_key("grok") == "xai-test-key-123"

        is_conf, src = CredentialManager.get_credential_source("grok")
        assert is_conf
        assert "MockKeyring" in src

        # Delete key
        assert CredentialManager.delete_api_key("grok") is True
        assert CredentialManager.get_api_key("grok") is None


def test_credential_manager_env_fallback():
    """Verify keyring takes precedence over environment variable fallback."""
    mock_store = {"support:api_key:gemini": "keyring-gemini-key"}

    with patch("keyring.get_password", side_effect=lambda s, a: mock_store.get(f"{s}:{a}")), \
         patch.object(CredentialManager, "is_keyring_available", return_value=True):

        # Keyring takes precedence
        os.environ["GEMINI_API_KEY"] = "env-gemini-key"
        assert CredentialManager.get_api_key("gemini") == "keyring-gemini-key"

        # Remove from keyring -> falls back to env var
        mock_store.clear()
        assert CredentialManager.get_api_key("gemini") == "env-gemini-key"
        is_conf, src = CredentialManager.get_credential_source("gemini")
        assert is_conf
        assert "GEMINI_API_KEY" in src

        # Remove env var -> unconfigured
        del os.environ["GEMINI_API_KEY"]
        assert CredentialManager.get_api_key("gemini") is None


def test_credential_manager_unavailable_keyring_raises():
    """Verify actionable error is raised if keyring is unavailable when saving."""
    with patch.object(CredentialManager, "is_keyring_available", return_value=False):
        with pytest.raises(KeyringUnavailableError) as exc_info:
            CredentialManager.set_api_key("gemini", "some-key")
        assert "No secure OS keyring backend available" in str(exc_info.value)


def test_auth_status_cli():
    """Verify support auth status renders table with correct status."""
    with patch.object(CredentialManager, "get_credential_source") as mock_src:
        mock_src.side_effect = lambda prov: (True, "Keyring (MockVault)") if prov == "gemini" else (False, "Not configured")

        res = runner.invoke(app, ["auth", "status"])
        assert res.exit_code == 0
        assert "Grok (xAI)" in res.stdout
        assert "Google Gemini" in res.stdout
        assert "CONFIGURED" in res.stdout
        assert "NOT CONFIGURED" in res.stdout
        assert "MockVault" in res.stdout


def test_auth_login_successful_validation():
    """Verify successful login validates and saves key to keyring."""
    valid_result = ProviderValidationResult(
        provider="gemini",
        status=ProviderStatus.AVAILABLE,
        message="Gemini API connected",
        latency_ms=120.0,
    )

    with patch.object(GeminiProvider, "validate_configuration", return_value=valid_result), \
         patch.object(CredentialManager, "set_api_key") as mock_set, \
         patch.object(CredentialManager, "get_backend_name", return_value="Windows WinVaultKeyring"):

        res = runner.invoke(app, ["auth", "login", "gemini"], input="AIzaSyD-valid-key\n")
        assert res.exit_code == 0
        assert "credentials validated and securely stored" in res.stdout
        assert "WinVaultKeyring" in res.stdout
        mock_set.assert_called_once_with("gemini", "AIzaSyD-valid-key")
        # Ensure raw key was never echoed
        assert "AIzaSyD-valid-key" not in res.stdout


def test_auth_login_failed_validation_does_not_save():
    """Verify failed authentication does not save key and exits with code 1."""
    failed_result = ProviderValidationResult(
        provider="grok",
        status=ProviderStatus.AUTH_FAILED,
        message="Authentication rejected (HTTP 401)",
        hint="Check that your XAI_API_KEY is valid.",
    )

    with patch.object(GrokProvider, "validate_configuration", return_value=failed_result), \
         patch.object(CredentialManager, "set_api_key") as mock_set:

        res = runner.invoke(app, ["auth", "login", "grok"], input="bad-key\n")
        assert res.exit_code == 1
        assert "Authentication failed" in res.stdout
        assert "Key was not saved" in res.stdout
        mock_set.assert_not_called()


def test_auth_test_cli():
    """Verify support auth test verifies credentials without changing configuration."""
    # 1. Unconfigured
    with patch.object(CredentialManager, "get_api_key", return_value=None):
        res = runner.invoke(app, ["auth", "test", "grok"])
        assert res.exit_code == 1
        assert "Provider 'grok' is not configured" in res.stdout

    # 2. Configured and valid
    valid_res = ProviderValidationResult(
        provider="grok",
        status=ProviderStatus.AVAILABLE,
        message="xAI Grok API connected and verified",
        latency_ms=85.0,
    )
    with patch.object(CredentialManager, "get_api_key", return_value="valid-key"), \
         patch.object(GrokProvider, "validate_configuration", return_value=valid_res):

        res = runner.invoke(app, ["auth", "test", "grok"])
        assert res.exit_code == 0
        assert "credentials and API connectivity verified" in res.stdout


def test_auth_logout_cli():
    """Verify support auth logout removes credential from keyring."""
    with patch.object(CredentialManager, "is_keyring_available", return_value=True), \
         patch("keyring.get_password", return_value="stored-key"), \
         patch.object(CredentialManager, "delete_api_key") as mock_del:

        res = runner.invoke(app, ["auth", "logout", "gemini", "--yes"])
        assert res.exit_code == 0
        assert "Removed stored credentials for Google Gemini from OS keyring" in res.stdout
        mock_del.assert_called_once_with("gemini")


def test_provider_adapters_use_credential_manager():
    """Verify GrokProvider and GeminiProvider read from CredentialManager automatically."""
    with patch.object(CredentialManager, "get_api_key") as mock_get:
        mock_get.side_effect = lambda p: f"keyring-{p}-secret"

        grok = GrokProvider(api_key=None)
        assert grok.api_key == "keyring-grok-secret"

        gemini = GeminiProvider(api_key=None)
        assert gemini.api_key == "keyring-gemini-secret"
