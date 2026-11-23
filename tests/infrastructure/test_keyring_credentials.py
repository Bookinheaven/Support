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
from tests.conftest import FakeKeyringBackend


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

    assert store.get_password(profile_id) is None
    assert not store.has_password(profile_id)

    store.store_password(profile_id, "my-secret-password")

    assert store.get_password(profile_id) == "my-secret-password"
    assert store.has_password(profile_id)


def test_missing_credential_returns_none() -> None:
    backend = FakeKeyringBackend()
    store = KeyringCredentialStore(backend=backend, allow_test_backend=True)
    non_existent_id = uuid4()

    assert store.get_password(non_existent_id) is None
    assert not store.has_password(non_existent_id)


def test_delete_and_idempotent_delete() -> None:
    backend = FakeKeyringBackend()
    store = KeyringCredentialStore(backend=backend, allow_test_backend=True)
    profile_id = uuid4()

    store.store_password(profile_id, "temporary-secret")
    assert store.has_password(profile_id)

    # First delete
    store.delete_password(profile_id)
    assert not store.has_password(profile_id)
    assert store.get_password(profile_id) is None

    # Second delete (idempotent: must not raise error)
    store.delete_password(profile_id)
    assert not store.has_password(profile_id)


def test_rejection_of_unapproved_backends() -> None:
    # 1. Null backend must be rejected
    null_store = KeyringCredentialStore(backend=null.Keyring())
    with pytest.raises(CredentialStoreUnavailableError, match="not an approved native secure"):
        null_store.store_password(uuid4(), "secret")

    # 2. Fail backend must be rejected
    fail_store = KeyringCredentialStore(backend=fail.Keyring())
    with pytest.raises(CredentialStoreUnavailableError, match="not an approved native secure"):
        fail_store.get_password(uuid4())

    # 3. Arbitrary custom/third-party backend without allow_test_backend must be rejected
    custom_backend = FakeKeyringBackend()
    custom_store = KeyringCredentialStore(backend=custom_backend, allow_test_backend=False)
    with pytest.raises(CredentialStoreUnavailableError, match="not an approved native secure"):
        custom_store.delete_password(uuid4())


def test_backend_failure_handling() -> None:
    broken = BrokenKeyringBackend()
    store = KeyringCredentialStore(backend=broken, allow_test_backend=True)
    profile_id = uuid4()

    with pytest.raises(CredentialStorageError, match="Failed to store credential"):
        store.store_password(profile_id, "password")

    with pytest.raises(CredentialRetrievalError, match="Failed to retrieve credential"):
        store.get_password(profile_id)

    with pytest.raises(CredentialDeletionError, match="Failed to delete credential"):
        store.delete_password(profile_id)


def test_allowlist_structure() -> None:
    expected = {
        ("keyring.backends.Windows", "WinVaultKeyring"),
        ("keyring.backends.macOS", "Keyring"),
        ("keyring.backends.SecretService", "Keyring"),
        ("keyring.backends.kwallet", "DBusKeyring"),
        ("keyring.backends.kwallet", "DBusKeyringKWallet4"),
    }
    assert APPROVED_BACKENDS == expected
