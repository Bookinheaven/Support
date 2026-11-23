"""Tests for KeyringCredentialStore and native backend allowlist policy."""

from uuid import uuid4

import keyring
import keyring.backend
import keyring.errors
from keyring.backends import fail, null
import pytest

from support.application.errors import (
    CredentialDeletionError,
    CredentialRetrievalError,
    CredentialStorageError,
    CredentialStoreUnavailableError,
)
from support.infrastructure.keyring_credentials import (
    APPROVED_BACKENDS,
    KeyringCredentialStore,
)


class FakeKeyringBackend(keyring.backend.KeyringBackend):
    """Deterministic in-memory keyring backend for tests."""

    priority = 1

    def __init__(self) -> None:
        self.passwords: dict[tuple[str, str], str] = {}

    def set_password(self, service: str, username: str, password: str) -> None:
        self.passwords[(service, username)] = password

    def get_password(self, service: str, username: str) -> str | None:
        return self.passwords.get((service, username))

    def delete_password(self, service: str, username: str) -> None:
        if (service, username) not in self.passwords:
            raise keyring.errors.PasswordDeleteError("Password not found")
        del self.passwords[(service, username)]


class BrokenKeyringBackend(keyring.backend.KeyringBackend):
    """Keyring backend that simulates OS store failures."""

    priority = 1

    def set_password(self, service: str, username: str, password: str) -> None:
        raise keyring.errors.PasswordSetError("Device I/O write error")

    def get_password(self, service: str, username: str) -> str | None:
        raise keyring.errors.KeyringError("Keyring locked or inaccessible")

    def delete_password(self, service: str, username: str) -> None:
        raise keyring.errors.KeyringError("Permission denied deleting secret")


def test_set_and_retrieve_credential() -> None:
    backend = FakeKeyringBackend()
    store = KeyringCredentialStore(backend=backend, allow_test_backend=True)
    profile_id = uuid4()

    assert store.get(profile_id) is None
    assert not store.contains(profile_id)

    store.set(profile_id, "my-secret-password")

    assert store.get(profile_id) == "my-secret-password"
    assert store.contains(profile_id)


def test_missing_credential_returns_none() -> None:
    backend = FakeKeyringBackend()
    store = KeyringCredentialStore(backend=backend, allow_test_backend=True)
    non_existent_id = uuid4()

    assert store.get(non_existent_id) is None
    assert not store.contains(non_existent_id)


def test_delete_and_idempotent_delete() -> None:
    backend = FakeKeyringBackend()
    store = KeyringCredentialStore(backend=backend, allow_test_backend=True)
    profile_id = uuid4()

    store.set(profile_id, "temporary-secret")
    assert store.contains(profile_id)

    # First delete
    store.delete(profile_id)
    assert not store.contains(profile_id)
    assert store.get(profile_id) is None

    # Second delete (idempotent: must not raise error)
    store.delete(profile_id)
    assert not store.contains(profile_id)


def test_rejection_of_unapproved_backends() -> None:
    # 1. Null backend must be rejected
    null_store = KeyringCredentialStore(backend=null.Keyring())
    with pytest.raises(CredentialStoreUnavailableError, match="not an approved native secure"):
        null_store.set(uuid4(), "secret")

    # 2. Fail backend must be rejected
    fail_store = KeyringCredentialStore(backend=fail.Keyring())
    with pytest.raises(CredentialStoreUnavailableError, match="not an approved native secure"):
        fail_store.get(uuid4())

    # 3. Arbitrary custom/third-party backend without allow_test_backend must be rejected
    custom_backend = FakeKeyringBackend()
    custom_store = KeyringCredentialStore(backend=custom_backend, allow_test_backend=False)
    with pytest.raises(CredentialStoreUnavailableError, match="not an approved native secure"):
        custom_store.delete(uuid4())


def test_backend_failure_handling() -> None:
    broken = BrokenKeyringBackend()
    store = KeyringCredentialStore(backend=broken, allow_test_backend=True)
    profile_id = uuid4()

    with pytest.raises(CredentialStorageError, match="Failed to store credential"):
        store.set(profile_id, "password")

    with pytest.raises(CredentialRetrievalError, match="Failed to retrieve credential"):
        store.get(profile_id)

    with pytest.raises(CredentialDeletionError, match="Failed to delete credential"):
        store.delete(profile_id)


def test_allowlist_structure() -> None:
    expected = {
        ("keyring.backends.Windows", "WinVaultKeyring"),
        ("keyring.backends.macOS", "Keyring"),
        ("keyring.backends.SecretService", "Keyring"),
        ("keyring.backends.kwallet", "DBusKeyring"),
        ("keyring.backends.kwallet", "DBusKeyringKWallet4"),
    }
    assert APPROVED_BACKENDS == expected
