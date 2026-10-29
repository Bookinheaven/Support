"""Secure credential storage using OS keyring with environment variable fallback."""

import os
import re
from typing import Optional
from support.core.logging import logger

SERVICE_NAME = "support-cli"


def _clean_env_name(profile_name: str) -> str:
    """Sanitize profile name for environment variable lookup."""
    clean = re.sub(r"[^A-Za-z0-9]", "_", profile_name).upper()
    return f"SUPPORT_DB_PASSWORD_{clean}"


class KeyringStore:
    """Manages secret credentials using OS secure storage or environment variable fallbacks."""

    @staticmethod
    def is_keyring_available() -> bool:
        """Check if an OS keyring backend is operational."""
        try:
            import keyring
            backend = keyring.get_keyring()
            # If backend is a dummy or fail backend (priority <= 0), it's not operational
            priority = getattr(backend, "priority", 1)
            return priority > 0
        except Exception:
            return False

    @staticmethod
    def set_password(profile_name: str, password: str) -> bool:
        """Save password securely in the OS credential store."""
        try:
            import keyring
            keyring.set_password(SERVICE_NAME, f"profile:{profile_name}", password)
            return True
        except Exception as e:
            logger.warning(
                f"[yellow]Failed to store password in OS keyring ({e}). "
                f"You can set the environment variable {_clean_env_name(profile_name)} instead.[/yellow]"
            )
            return False

    @staticmethod
    def get_password(profile_name: str) -> Optional[str]:
        """Retrieve password from OS credential store, falling back to environment variables."""
        # 1. Attempt keyring lookup
        try:
            import keyring
            stored = keyring.get_password(SERVICE_NAME, f"profile:{profile_name}")
            if stored is not None:
                return stored
        except Exception as e:
            logger.debug(f"Keyring lookup failed for '{profile_name}': {e}")

        # 2. Check environment variable fallback
        env_var = _clean_env_name(profile_name)
        env_val = os.getenv(env_var)
        if env_val:
            return env_val

        return None

    @staticmethod
    def delete_password(profile_name: str) -> bool:
        """Remove password from OS credential store."""
        try:
            import keyring
            keyring.delete_password(SERVICE_NAME, f"profile:{profile_name}")
            return True
        except Exception:
            # Key might not exist in keyring, which is safe to ignore
            return False

    @staticmethod
    def get_credential_status(profile_name: str) -> str:
        """Return diagnostic status of password storage without exposing the secret."""
        try:
            import keyring
            if keyring.get_password(SERVICE_NAME, f"profile:{profile_name}"):
                return "Keyring (Secure)"
        except Exception:
            pass

        env_var = _clean_env_name(profile_name)
        if os.getenv(env_var):
            return f"Env ({env_var})"

        return "None"
