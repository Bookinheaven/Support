"""Secure credential manager for Support LLM cloud providers using OS keyring.

Provides secure storage and retrieval of API keys across:
- Windows: Windows Credential Manager (WinVaultKeyring)
- macOS: Apple Keychain
- Linux: Secret Service / KWallet

Supports environment variable fallbacks for CI/CD and headless environments.
Never persists API keys in application configuration or plain text files.
"""

import os
from typing import Optional, Tuple
from support.core.exceptions import KeyringUnavailableError
from support.core.logging import logger

SERVICE_NAME = "support"

# Provider identifier mapping to environment variable and keyring account
PROVIDER_CREDENTIALS = {
    "grok": {
        "env_var": "XAI_API_KEY",
        "keyring_account": "api_key:grok",
        "display_name": "Grok (xAI)",
    },
    "gemini": {
        "env_var": "GEMINI_API_KEY",
        "keyring_account": "api_key:gemini",
        "display_name": "Google Gemini",
    },
}


def redact_api_key(key: Optional[str]) -> str:
    """Safely redact API key for logs or display without revealing the secret."""
    if not key:
        return ""
    stripped = key.strip()
    if len(stripped) <= 6:
        return "******"
    return f"{stripped[:3]}...{stripped[-3:]}"


class CredentialManager:
    """Manages secret credentials using OS secure storage with environment variable fallbacks."""

    SERVICE_NAME = SERVICE_NAME

    @classmethod
    def is_keyring_available(cls) -> bool:
        """Check if an OS keyring backend is operational with positive priority."""
        try:
            import keyring
            backend = keyring.get_keyring()
            priority = getattr(backend, "priority", 1)
            # Priority <= 0 indicates null, fail, or disabled keyring backends
            return priority > 0
        except Exception:
            return False

    @classmethod
    def get_backend_name(cls) -> str:
        """Return human-readable name of the active OS keyring backend."""
        try:
            import keyring
            backend = keyring.get_keyring()
            name = getattr(backend, "name", None)
            if name:
                return name
            return type(backend).__name__
        except Exception:
            return "Unavailable"

    @classmethod
    def get_api_key(cls, provider: str) -> Optional[str]:
        """Resolve API key for a provider.

        Resolution order:
        1. Key stored in OS Keyring (via 'support auth login')
        2. Environment variable fallback (XAI_API_KEY, GEMINI_API_KEY)
        """
        provider_key = provider.lower()
        cfg = PROVIDER_CREDENTIALS.get(provider_key)
        if not cfg:
            return None

        # 1. Attempt lookup from OS Keyring
        if cls.is_keyring_available():
            try:
                import keyring
                stored = keyring.get_password(cls.SERVICE_NAME, cfg["keyring_account"])
                if stored and stored.strip():
                    return stored.strip()
            except Exception as e:
                logger.debug(f"OS keyring read failed for provider '{provider}': {e}")

        # 2. Fall back to environment variable
        env_val = os.getenv(cfg["env_var"])
        if env_val and env_val.strip():
            return env_val.strip()

        return None

    @classmethod
    def get_credential_source(cls, provider: str) -> Tuple[bool, str]:
        """Determine whether provider is configured and return source description.

        Returns:
            Tuple of (is_configured: bool, source_description: str)
        """
        provider_key = provider.lower()
        cfg = PROVIDER_CREDENTIALS.get(provider_key)
        if not cfg:
            return False, "Unsupported provider"

        # Check OS Keyring
        if cls.is_keyring_available():
            try:
                import keyring
                stored = keyring.get_password(cls.SERVICE_NAME, cfg["keyring_account"])
                if stored and stored.strip():
                    backend_name = cls.get_backend_name()
                    return True, f"Keyring ({backend_name})"
            except Exception:
                pass

        # Check Environment variable
        env_val = os.getenv(cfg["env_var"])
        if env_val and env_val.strip():
            return True, f"Environment ({cfg['env_var']})"

        return False, "Not configured"

    @classmethod
    def set_api_key(cls, provider: str, api_key: str) -> None:
        """Store API key securely in the OS credential store.

        Raises:
            ValueError: If provider is unsupported or api_key is empty.
            KeyringUnavailableError: If OS keyring backend is unavailable or write fails.
        """
        provider_key = provider.lower()
        cfg = PROVIDER_CREDENTIALS.get(provider_key)
        if not cfg:
            raise ValueError(f"Provider '{provider}' does not support API key authentication.")

        clean_key = api_key.strip()
        if not clean_key:
            raise ValueError("API key cannot be empty.")

        if not cls.is_keyring_available():
            raise KeyringUnavailableError(
                f"No secure OS keyring backend available. "
                f"On headless Linux, ensure 'secret-service' / 'gnome-keyring' is operational, "
                f"or set the {cfg['env_var']} environment variable instead."
            )

        try:
            import keyring
            keyring.set_password(cls.SERVICE_NAME, cfg["keyring_account"], clean_key)
        except Exception as e:
            raise KeyringUnavailableError(
                f"Failed to store credential in OS keyring: {e}. "
                f"You can set {cfg['env_var']} as an alternative."
            )

    @classmethod
    def delete_api_key(cls, provider: str) -> bool:
        """Remove API key from the OS credential store.

        Returns:
            True if removed or already absent; False if provider unsupported.
        """
        provider_key = provider.lower()
        cfg = PROVIDER_CREDENTIALS.get(provider_key)
        if not cfg:
            return False

        if not cls.is_keyring_available():
            return False

        try:
            import keyring
            keyring.delete_password(cls.SERVICE_NAME, cfg["keyring_account"])
            return True
        except Exception:
            # Key might not exist in keyring, which is considered successfully absent
            return True
