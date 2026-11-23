"""Tests for ConnectionTestWorkflow application component."""

from pathlib import Path
from unittest.mock import MagicMock
from uuid import uuid4

import keyring.backend
import keyring.errors
import pytest

from support.application.connection import ConnectionTestWorkflow
from support.application.errors import (
    AuthenticationFailedError,
    ProfileNotFoundError,
)
from support.application.ports import ConnectionTestResult
from support.domain.profiles import DatabaseProfile, PostgresConnection
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


def _create_workflow(tmp_path: Path) -> tuple[ConnectionTestWorkflow, SQLiteProfileRepository, KeyringCredentialStore, MagicMock]:
    repo = SQLiteProfileRepository(tmp_path / "profiles.sqlite3")
    backend = FakeKeyringBackend()
    cred_store = KeyringCredentialStore(backend=backend, allow_test_backend=True)
    connector = MagicMock()
    workflow = ConnectionTestWorkflow(
        repository=repo,
        connector=connector,
        credential_store=cred_store,
    )
    return workflow, repo, cred_store, connector


def test_test_profile_nonexistent_raises_error(tmp_path: Path) -> None:
    workflow, _, _, _ = _create_workflow(tmp_path)

    with pytest.raises(ProfileNotFoundError, match="Profile 'nonexistent' not found"):
        workflow.test_profile_connection("nonexistent")


def test_test_profile_connection_success(tmp_path: Path) -> None:
    workflow, repo, cred_store, connector = _create_workflow(tmp_path)

    conn = PostgresConnection(database="app_db", username="app_user", ssl_mode="prefer")
    profile = DatabaseProfile(name="test-profile", connection=conn)
    repo.add(profile)
    cred_store.set(profile.id, "saved-secret")

    expected_result = ConnectionTestResult(
        success=True,
        database_type="postgresql",
        database="app_db",
        username="app_user",
        host="localhost",
        port=5432,
        ssl_mode="prefer",
        server_version="16.1",
    )
    connector.test_connection.return_value = expected_result

    # Resolve saved password
    resolved_pw = workflow.resolve_saved_password(profile.id)
    assert resolved_pw == "saved-secret"

    result = workflow.test_profile_connection("test-profile", password=resolved_pw, used_saved_password=True)
    assert result == expected_result
    connector.test_connection.assert_called_once_with(conn, password="saved-secret")


def test_test_profile_preserves_used_saved_password_on_auth_failure(tmp_path: Path) -> None:
    workflow, repo, _, connector = _create_workflow(tmp_path)

    conn = PostgresConnection(database="app_db", username="app_user", ssl_mode="prefer")
    profile = DatabaseProfile(name="fail-auth", connection=conn)
    repo.add(profile)

    connector.test_connection.side_effect = AuthenticationFailedError("Bad password")

    with pytest.raises(AuthenticationFailedError) as exc_info:
        workflow.test_profile_connection("fail-auth", password="wrong", used_saved_password=True)

    assert exc_info.value.used_saved_password is True
