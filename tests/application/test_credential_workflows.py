"""Tests for credential workflows in ProfileService."""

from pathlib import Path
from unittest.mock import MagicMock
from uuid import UUID, uuid4

import keyring.backend
import keyring.errors
import pytest

from support.application.errors import (
    CredentialDeletionError,
    ProfileNotFoundError,
    ProfileStorageError,
)
from support.application.service import ProfileService
from support.infrastructure.keyring_credentials import KeyringCredentialStore
from support.infrastructure.sqlite_profiles import SQLiteProfileRepository


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


def _create_service(tmp_path: Path) -> tuple[ProfileService, SQLiteProfileRepository, KeyringCredentialStore]:
    repo = SQLiteProfileRepository(tmp_path / "profiles.sqlite3")
    backend = FakeKeyringBackend()
    cred_store = KeyringCredentialStore(backend=backend, allow_test_backend=True)
    service = ProfileService(repository=repo, credential_store=cred_store)
    return service, repo, cred_store


def test_nonexistent_profile_password_operations_rejected(tmp_path: Path) -> None:
    service, _, _ = _create_service(tmp_path)

    with pytest.raises(ProfileNotFoundError, match="Profile 'unknown' not found"):
        service.set_profile_password("unknown", "secret")

    with pytest.raises(ProfileNotFoundError, match="Profile 'unknown' not found"):
        service.get_profile_password("unknown")

    with pytest.raises(ProfileNotFoundError, match="Profile 'unknown' not found"):
        service.has_profile_password("unknown")

    with pytest.raises(ProfileNotFoundError, match="Profile 'unknown' not found"):
        service.remove_profile_password("unknown")


def test_profile_rename_preserves_credential_lookup(tmp_path: Path) -> None:
    service, _, cred_store = _create_service(tmp_path)

    profile = service.add_profile(
        name="initial-name",
        database="db",
        username="user",
        ssl_mode="prefer",
    )
    service.set_profile_password("initial-name", "super-secret")

    # Verify stored by profile.id
    assert cred_store.get(profile.id) == "super-secret"
    assert service.has_profile_password("initial-name")

    # Rename profile
    renamed = service.rename_profile("initial-name", "renamed-profile")
    assert renamed.id == profile.id

    # Password is accessible under new name
    assert service.get_profile_password("renamed-profile") == "super-secret"
    assert service.has_profile_password("renamed-profile")


def test_profile_deletion_cleans_up_credential(tmp_path: Path) -> None:
    service, repo, cred_store = _create_service(tmp_path)

    profile = service.add_profile(
        name="to-delete",
        database="db",
        username="user",
        ssl_mode="prefer",
    )
    service.set_profile_password("to-delete", "pwd123")
    assert cred_store.contains(profile.id)

    service.remove_profile("to-delete")

    assert repo.get_by_name("to-delete") is None
    assert not cred_store.contains(profile.id)
    assert cred_store.get(profile.id) is None


def test_profile_deletion_aborts_if_credential_deletion_fails(tmp_path: Path) -> None:
    service, repo, cred_store = _create_service(tmp_path)

    profile = service.add_profile(
        name="keep-me",
        database="db",
        username="user",
        ssl_mode="prefer",
    )
    service.set_profile_password("keep-me", "pwd123")

    # Simulate credential store failure during deletion
    cred_store.delete = MagicMock(side_effect=CredentialDeletionError("OS keychain unavailable"))

    with pytest.raises(CredentialDeletionError, match="OS keychain unavailable"):
        service.remove_profile("keep-me")

    # SQLite profile MUST be retained
    assert repo.get_by_name("keep-me") is not None


def test_profile_deletion_reports_sqlite_failure_after_credential_deletion(tmp_path: Path) -> None:
    service, repo, cred_store = _create_service(tmp_path)

    profile = service.add_profile(
        name="fail-sqlite",
        database="db",
        username="user",
        ssl_mode="prefer",
    )
    service.set_profile_password("fail-sqlite", "pwd123")

    # Simulate SQLite failure after credential deletion
    repo.delete = MagicMock(side_effect=RuntimeError("disk write error"))

    with pytest.raises(ProfileStorageError, match="password was removed, but removing profile metadata failed"):
        service.remove_profile("fail-sqlite")

    # Credential was removed
    assert not cred_store.contains(profile.id)
