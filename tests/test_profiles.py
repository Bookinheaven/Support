"""Tests for connection profile models, persistence manager, and keyring store."""

from pathlib import Path
import pytest
from support.core.dialect import Dialect
from support.db.models import ConnectionProfile
from support.db.manager import ProfileManager, ProfileAlreadyExistsError, ProfileNotFoundError
from support.db.keyring_store import KeyringStore


@pytest.fixture
def temp_profile_manager(tmp_path: Path) -> ProfileManager:
    """Fixture providing a ProfileManager isolated in a temporary directory."""
    return ProfileManager(config_dir=tmp_path / ".support")


def test_connection_profile_redacted_url():
    profile_pg = ConnectionProfile(
        name="my-pg",
        dialect=Dialect.POSTGRES,
        host="localhost",
        port=5432,
        database="mydb",
        username="dbuser",
    )
    redacted = profile_pg.get_redacted_url()
    assert "postgresql+psycopg://" in redacted
    assert "dbuser:***@localhost:5432/mydb" in redacted

    profile_sqlite = ConnectionProfile(
        name="my-sqlite",
        dialect=Dialect.SQLITE,
        database="d:/data/app.db",
    )
    assert profile_sqlite.get_redacted_url() == "sqlite:///d:/data/app.db"


def test_profile_manager_lifecycle(temp_profile_manager: ProfileManager):
    profile = ConnectionProfile(
        name="dev-postgres",
        dialect=Dialect.POSTGRES,
        host="127.0.0.1",
        port=5433,
        database="support_dev",
        username="support_dev",
    )

    # 1. Save profile
    temp_profile_manager.save_profile(profile, password="secret_pass_123")
    retrieved = temp_profile_manager.get_profile("dev-postgres")
    assert retrieved is not None
    assert retrieved.name == "dev-postgres"
    assert retrieved.port == 5433

    # Password should NOT be in the JSON file
    profiles_json_content = temp_profile_manager.profiles_file.read_text(encoding="utf-8")
    assert "secret_pass_123" not in profiles_json_content

    # 2. Duplicate profile without overwrite should raise error
    with pytest.raises(ProfileAlreadyExistsError):
        temp_profile_manager.save_profile(profile, overwrite=False)

    # 3. Overwrite profile
    profile.port = 5434
    temp_profile_manager.save_profile(profile, overwrite=True)
    updated = temp_profile_manager.get_profile("dev-postgres")
    assert updated is not None
    assert updated.port == 5434

    # 4. List profiles
    profiles_list = temp_profile_manager.list_profiles()
    assert len(profiles_list) == 1
    assert profiles_list[0].name == "dev-postgres"

    # 5. Delete profile
    deleted = temp_profile_manager.delete_profile("dev-postgres")
    assert deleted is True
    assert temp_profile_manager.get_profile("dev-postgres") is None

    # Deleting non-existent raises error
    with pytest.raises(ProfileNotFoundError):
        temp_profile_manager.delete_profile("dev-postgres")


def test_keyring_environment_variable_fallback(monkeypatch):
    profile_name = "test_env_fallback"
    env_var = "SUPPORT_DB_PASSWORD_TEST_ENV_FALLBACK"

    monkeypatch.setenv(env_var, "env_secret_999")
    retrieved = KeyringStore.get_password(profile_name)
    assert retrieved == "env_secret_999"

    status = KeyringStore.get_credential_status(profile_name)
    assert "Env" in status
