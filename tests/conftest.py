"""Global pytest fixtures and test configuration."""

import pytest
import keyring.backend
import keyring.errors

from support.infrastructure.keyring_credentials import KeyringCredentialStore


class GlobalFakeKeyringBackend(keyring.backend.KeyringBackend):
    """Deterministic in-memory keyring backend for automated CLI tests."""

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


@pytest.fixture(autouse=True)
def isolated_cli_credential_store(monkeypatch: pytest.MonkeyPatch) -> KeyringCredentialStore:
    """Ensure no CLI test ever calls the real OS credential store."""
    backend = GlobalFakeKeyringBackend()
    fake_store = KeyringCredentialStore(backend=backend, allow_test_backend=True)
    monkeypatch.setattr(
        "support.cli.profile.KeyringCredentialStore",
        lambda *args, **kwargs: fake_store,
    )
    return fake_store
