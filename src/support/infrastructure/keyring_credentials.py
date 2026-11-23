"""OS-native secure credential storage using Python keyring."""

from __future__ import annotations

from typing import TYPE_CHECKING
from uuid import UUID

import keyring
import keyring.backend
import keyring.errors

from support.application.errors import (
    CredentialDeletionError,
    CredentialRetrievalError,
    CredentialStorageError,
    CredentialStoreUnavailableError,
)

if TYPE_CHECKING:
    from keyring.backend import KeyringBackend

SUPPORT_KEYRING_SERVICE: str = "support.database"

APPROVED_BACKENDS: frozenset[tuple[str, str]] = frozenset(
    {
        ("keyring.backends.Windows", "WinVaultKeyring"),
        ("keyring.backends.macOS", "Keyring"),
        ("keyring.backends.SecretService", "Keyring"),
        ("keyring.backends.kwallet", "DBusKeyring"),
        ("keyring.backends.kwallet", "DBusKeyringKWallet4"),
    }
)


class KeyringCredentialStore:
    """Secure credential store implementation backed by approved OS-native keyrings."""

    def __init__(
        self,
        backend: KeyringBackend | None = None,
        service_name: str = SUPPORT_KEYRING_SERVICE,
        allow_test_backend: bool = False,
    ) -> None:
        self._backend = backend
        self._service_name = service_name
        self._allow_test_backend = allow_test_backend

    def _ensure_backend(self) -> KeyringBackend:
        """Resolve and validate that the active keyring backend is an approved native store."""
        backend = self._backend
        if backend is None:
            backend = keyring.get_keyring()
            self._backend = backend

        if not self._allow_test_backend:
            if not isinstance(backend, keyring.backend.KeyringBackend):
                raise CredentialStoreUnavailableError(
                    f"Active backend '{type(backend).__name__}' does not inherit from KeyringBackend."
                )

            cls = type(backend)
            module_name = cls.__module__.removesuffix(".__init__")
            backend_id = (module_name, cls.__qualname__)

            if backend_id not in APPROVED_BACKENDS:
                raise CredentialStoreUnavailableError(
                    f"Active keyring backend '{module_name}.{cls.__qualname__}' is not an approved "
                    "native secure credential store. Supported native stores are Windows Credential Locker, "
                    "macOS Keychain, Linux Secret Service, and Linux KDE Wallet."
                )

        return backend

    def set(self, profile_id: UUID, password: str) -> None:
        """Securely store a password for the given profile ID."""
        backend = self._ensure_backend()
        account = str(profile_id)
        try:
            backend.set_password(self._service_name, account, password)
        except keyring.errors.KeyringError as exc:
            raise CredentialStorageError(
                f"Failed to store credential in native secure store: {exc}"
            ) from exc
        except Exception as exc:
            raise CredentialStorageError(
                "Unexpected failure storing credential in native secure store."
            ) from exc

    def get(self, profile_id: UUID) -> str | None:
        """Retrieve the password for the given profile ID, or None if absent."""
        backend = self._ensure_backend()
        account = str(profile_id)
        try:
            return backend.get_password(self._service_name, account)
        except keyring.errors.KeyringError as exc:
            raise CredentialRetrievalError(
                f"Failed to retrieve credential from native secure store: {exc}"
            ) from exc
        except Exception as exc:
            raise CredentialRetrievalError(
                "Unexpected failure retrieving credential from native secure store."
            ) from exc

    def delete(self, profile_id: UUID) -> None:
        """Delete the password for the given profile ID. Idempotent: absent credentials do not fail."""
        backend = self._ensure_backend()
        account = str(profile_id)
        try:
            backend.delete_password(self._service_name, account)
        except keyring.errors.PasswordDeleteError as exc:
            # Idempotent delete: verify if password is absent
            try:
                remaining = backend.get_password(self._service_name, account)
            except Exception:
                remaining = None
            if remaining is None:
                return
            raise CredentialDeletionError(
                f"Failed to delete credential from native secure store: {exc}"
            ) from exc
        except keyring.errors.KeyringError as exc:
            raise CredentialDeletionError(
                f"Failed to delete credential from native secure store: {exc}"
            ) from exc
        except Exception as exc:
            raise CredentialDeletionError(
                "Unexpected failure deleting credential from native secure store."
            ) from exc

    def contains(self, profile_id: UUID) -> bool:
        """Check whether a password exists for the given profile ID without revealing it."""
        return self.get(profile_id) is not None

    # Protocol method aliases
    store_password = set
    get_password = get
    delete_password = delete
    has_password = contains
