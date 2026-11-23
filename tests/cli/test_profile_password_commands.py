"""CLI tests for support profile password command group."""

from pathlib import Path
from unittest.mock import patch
import keyring.backend
import keyring.errors
from typer.testing import CliRunner

from support.cli.main import app
from support.infrastructure.keyring_credentials import KeyringCredentialStore


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

runner = CliRunner()


def test_profile_password_cli_full_workflow(tmp_path: Path) -> None:
    env = {"SUPPORT_HOME": str(tmp_path)}
    backend = FakeKeyringBackend()
    test_cred_store = KeyringCredentialStore(backend=backend, allow_test_backend=True)

    # 1. Add profile
    res = runner.invoke(
        app,
        ["profile", "add", "--name", "prod-db", "--database", "production", "--username", "admin", "--ssl-mode", "require"],
        env=env,
    )
    assert res.exit_code == 0

    with patch("support.cli.composition.KeyringCredentialStore", return_value=test_cred_store):
        # 2. Check initial password status (no password)
        res = runner.invoke(app, ["profile", "password", "status", "prod-db"], env=env)
        assert res.exit_code == 0
        assert "Profile 'prod-db': No saved password." in res.output

        # 3. Set password with mismatched confirmation
        res = runner.invoke(
            app,
            ["profile", "password", "set", "prod-db"],
            input="first_attempt\nsecond_mismatch\n",
            env=env,
        )
        assert res.exit_code == 1
        assert "Error: Passwords do not match." in res.output
        assert "first_attempt" not in res.output
        assert "second_mismatch" not in res.output

        # 4. Set password successfully with hidden input
        secret_password = "very-secret-password-12345"
        res = runner.invoke(
            app,
            ["profile", "password", "set", "prod-db"],
            input=f"{secret_password}\n{secret_password}\n",
            env=env,
        )
        assert res.exit_code == 0
        assert "Password for profile 'prod-db' stored securely." in res.output
        # CRITICAL SECURITY CHECK: password must NOT appear in output!
        assert secret_password not in res.output

        # 5. Check password status (present)
        res = runner.invoke(app, ["profile", "password", "status", "prod-db"], env=env)
        assert res.exit_code == 0
        assert "Profile 'prod-db': Saved password is present." in res.output
        assert secret_password not in res.output

        # 6. Remove password
        res = runner.invoke(app, ["profile", "password", "remove", "prod-db"], env=env)
        assert res.exit_code == 0
        assert "Password for profile 'prod-db' removed." in res.output

        # 7. Check password status again (removed)
        res = runner.invoke(app, ["profile", "password", "status", "prod-db"], env=env)
        assert res.exit_code == 0
        assert "Profile 'prod-db': No saved password." in res.output


def test_profile_password_cli_unavailable_store_handling(tmp_path: Path) -> None:
    env = {"SUPPORT_HOME": str(tmp_path)}

    # Add profile
    runner.invoke(
        app,
        ["profile", "add", "--name", "ci-db", "--database", "test", "--username", "u", "--ssl-mode", "prefer"],
        env=env,
    )

    from keyring.backends import null
    unapproved_store = KeyringCredentialStore(backend=null.Keyring())

    with patch("support.cli.composition.KeyringCredentialStore", return_value=unapproved_store):
        res = runner.invoke(
            app,
            ["profile", "password", "set", "ci-db"],
            input="pass\npass\n",
            env=env,
        )
        assert res.exit_code == 1
        assert "Error: Active keyring backend" in res.output
        assert "is not an approved native secure credential store" in res.output
        assert "Traceback" not in res.output


def test_profile_password_cli_nonexistent_profile(tmp_path: Path) -> None:
    env = {"SUPPORT_HOME": str(tmp_path)}
    backend = FakeKeyringBackend()
    test_cred_store = KeyringCredentialStore(backend=backend, allow_test_backend=True)

    with patch("support.cli.composition.KeyringCredentialStore", return_value=test_cred_store):
        res = runner.invoke(app, ["profile", "password", "status", "missing-db"], env=env)
        assert res.exit_code == 1
        assert "Error: Profile 'missing-db' not found." in res.output
        assert "Traceback" not in res.output

        res = runner.invoke(app, ["profile", "password", "remove", "missing-db"], env=env)
        assert res.exit_code == 1
        assert "Error: Profile 'missing-db' not found." in res.output
        assert "Traceback" not in res.output
