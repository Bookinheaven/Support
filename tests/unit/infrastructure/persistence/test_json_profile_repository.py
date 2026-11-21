"""Unit tests for JsonProfileRepository infrastructure persistence."""

import json
import os
from pathlib import Path
from unittest.mock import patch
from uuid import UUID, uuid4

import pytest

from support.domain.database.errors import (
    DuplicateProfileIdError,
    ProfileAlreadyExistsError,
    ProfileNotFoundError,
)
from support.domain.database.profile import DatabaseProfile
from support.infrastructure.persistence.json_profile_repository import (
    CURRENT_STORAGE_VERSION,
    JsonProfileRepository,
    StorageCorruptedError,
    StorageError,
    StorageVersionError,
)


def _sample_profile(name: str = "local-dev", **kwargs) -> DatabaseProfile:
    defaults = {
        "name": name,
        "dialect": "postgresql",
        "host": "localhost",
        "port": 5432,
        "database": "myapp",
        "username": "postgres",
        "sslmode": None,
    }
    defaults.update(kwargs)
    return DatabaseProfile(**defaults)


def test_save_and_load_profile(tmp_path: Path) -> None:
    store_file = tmp_path / "profiles.json"
    repo = JsonProfileRepository(store_file)
    p = _sample_profile(name="local-dev")

    repo.add(p)

    assert store_file.exists()
    content = json.loads(store_file.read_text(encoding="utf-8"))
    assert content["version"] == CURRENT_STORAGE_VERSION
    assert len(content["profiles"]) == 1
    assert content["profiles"][0]["id"] == str(p.id)
    assert content["profiles"][0]["name"] == "local-dev"

    # Reload from disk using new repository instance
    new_repo = JsonProfileRepository(store_file)
    loaded = new_repo.get_by_id(p.id)
    assert loaded is not None
    assert loaded.id == p.id
    assert loaded.name == "local-dev"
    assert loaded.host == "localhost"
    assert loaded.port == 5432


def test_stable_ids_across_reloads(tmp_path: Path) -> None:
    store_file = tmp_path / "profiles.json"
    repo1 = JsonProfileRepository(store_file)
    p = _sample_profile(name="staging")
    repo1.add(p)

    repo2 = JsonProfileRepository(store_file)
    loaded2 = repo2.get_by_name("staging")
    assert loaded2 is not None

    repo3 = JsonProfileRepository(store_file)
    loaded3 = repo3.get_by_name("staging")
    assert loaded3 is not None

    assert loaded2.id == p.id
    assert loaded3.id == p.id


def test_list_multiple_profiles(tmp_path: Path) -> None:
    repo = JsonProfileRepository(tmp_path / "profiles.json")
    repo.add(_sample_profile(name="dev-1"))
    repo.add(_sample_profile(name="dev-2"))
    repo.add(_sample_profile(name="dev-3"))

    profiles = repo.list_all()
    assert len(profiles) == 3
    names = {p.name for p in profiles}
    assert names == {"dev-1", "dev-2", "dev-3"}


def test_reject_duplicate_names_case_insensitive(tmp_path: Path) -> None:
    repo = JsonProfileRepository(tmp_path / "profiles.json")
    repo.add(_sample_profile(name="local-dev"))

    with pytest.raises(ProfileAlreadyExistsError, match="already exists"):
        repo.add(_sample_profile(name="LOCAL-DEV"))


def test_update_profile_preserves_id(tmp_path: Path) -> None:
    repo = JsonProfileRepository(tmp_path / "profiles.json")
    p = _sample_profile(name="old-name")
    repo.add(p)

    from dataclasses import replace
    updated = replace(p, name="renamed")
    repo.update(updated)

    assert repo.get_by_name("old-name") is None
    loaded = repo.get_by_name("renamed")
    assert loaded is not None
    assert loaded.id == p.id


def test_remove_profile_by_id(tmp_path: Path) -> None:
    repo = JsonProfileRepository(tmp_path / "profiles.json")
    p = _sample_profile(name="staging")
    repo.add(p)
    assert len(repo.list_all()) == 1

    repo.remove(p.id)
    assert len(repo.list_all()) == 0

    with pytest.raises(ProfileNotFoundError, match="not found"):
        repo.remove(p.id)


def test_missing_configuration_file(tmp_path: Path) -> None:
    non_existent = tmp_path / "missing_subdir" / "profiles.json"
    repo = JsonProfileRepository(non_existent)

    assert repo.list_all() == []
    assert repo.get_by_id(uuid4()) is None
    assert repo.get_by_name("anything") is None


def test_migration_version_1_to_version_2(tmp_path: Path) -> None:
    """Ensure legacy version 1 configurations without IDs are migrated seamlessly."""
    store_file = tmp_path / "profiles.json"
    v1_data = {
        "version": 1,
        "profiles": [
            {
                "name": "local-dev",
                "dialect": "postgresql",
                "host": "localhost",
                "port": 5432,
                "database": "myapp",
                "username": "postgres",
                "sslmode": None,
            },
            {
                "name": "staging",
                "dialect": "postgresql",
                "host": "staging.internal",
                "port": 5432,
                "database": "staging_db",
                "username": "deploy",
                "sslmode": "require",
            },
        ],
    }
    store_file.write_text(json.dumps(v1_data, indent=2), encoding="utf-8")

    # Load via repository, triggering migration
    repo = JsonProfileRepository(store_file)
    profiles = repo.list_all()

    assert len(profiles) == 2
    p_dev = repo.get_by_name("local-dev")
    p_stage = repo.get_by_name("staging")

    assert p_dev is not None
    assert p_stage is not None
    assert isinstance(p_dev.id, UUID)
    assert isinstance(p_stage.id, UUID)
    assert p_dev.id != p_stage.id
    assert p_stage.sslmode == "require"

    # Check that disk file is now version 2
    migrated_file_data = json.loads(store_file.read_text(encoding="utf-8"))
    assert migrated_file_data["version"] == 2
    assert len(migrated_file_data["profiles"]) == 2
    assert migrated_file_data["profiles"][0]["id"] == str(p_dev.id)

    # Re-reading should maintain the exact same stable IDs
    repo_reloaded = JsonProfileRepository(store_file)
    assert repo_reloaded.get_by_name("local-dev").id == p_dev.id
    assert repo_reloaded.get_by_name("staging").id == p_stage.id


def test_migration_failure_preserves_v1_file(tmp_path: Path) -> None:
    store_file = tmp_path / "profiles.json"
    v1_text = json.dumps({"version": 1, "profiles": [_sample_profile(name="p1").to_dict()]})
    store_file.write_text(v1_text, encoding="utf-8")

    repo = JsonProfileRepository(store_file)
    with patch("pathlib.Path.replace", side_effect=OSError("Disk write error")):
        with pytest.raises(StorageError, match="Failed to write profiles atomically"):
            repo.list_all()

    # Original v1 file must be intact
    assert store_file.read_text(encoding="utf-8") == v1_text


def test_malformed_json_handling(tmp_path: Path) -> None:
    store_file = tmp_path / "profiles.json"
    bad_json = "{ invalid json content..."
    store_file.write_text(bad_json, encoding="utf-8")
    repo = JsonProfileRepository(store_file)

    with pytest.raises(StorageCorruptedError, match="Malformed JSON"):
        repo.list_all()

    # File must not be overwritten
    assert store_file.read_text(encoding="utf-8") == bad_json


def test_unsupported_version_handling(tmp_path: Path) -> None:
    store_file = tmp_path / "profiles.json"
    unsupported = {"version": 999, "profiles": []}
    store_file.write_text(json.dumps(unsupported), encoding="utf-8")
    repo = JsonProfileRepository(store_file)

    with pytest.raises(StorageVersionError, match="Unsupported configuration version '999'"):
        repo.list_all()


def test_duplicate_id_in_v2_storage_rejected(tmp_path: Path) -> None:
    store_file = tmp_path / "profiles.json"
    shared_id = str(uuid4())
    bad_v2 = {
        "version": 2,
        "profiles": [
            {
                "id": shared_id,
                "name": "dev-1",
                "dialect": "postgresql",
                "host": "localhost",
                "port": 5432,
                "database": "myapp",
                "username": "postgres",
                "sslmode": None,
            },
            {
                "id": shared_id,
                "name": "dev-2",
                "dialect": "postgresql",
                "host": "localhost",
                "port": 5432,
                "database": "myapp",
                "username": "postgres",
                "sslmode": None,
            },
        ],
    }
    store_file.write_text(json.dumps(bad_v2), encoding="utf-8")
    repo = JsonProfileRepository(store_file)

    with pytest.raises(StorageCorruptedError, match="Duplicate profile ID"):
        repo.list_all()


def test_atomic_write_preserves_previous_config_on_failure(tmp_path: Path) -> None:
    store_file = tmp_path / "profiles.json"
    repo = JsonProfileRepository(store_file)
    p1 = _sample_profile(name="initial-profile")
    repo.add(p1)

    initial_content = store_file.read_text(encoding="utf-8")
    p2 = _sample_profile(name="second-profile")

    with patch("pathlib.Path.replace", side_effect=OSError("Disk full")):
        with pytest.raises(StorageError, match="Failed to write profiles atomically"):
            repo.add(p2)

    assert store_file.read_text(encoding="utf-8") == initial_content
    temp_files = list(tmp_path.glob(".profiles_tmp_*"))
    assert temp_files == []


def test_passwords_cannot_be_persisted(tmp_path: Path) -> None:
    store_file = tmp_path / "profiles.json"
    repo = JsonProfileRepository(store_file)
    repo.add(_sample_profile(name="local-dev"))

    raw_text = store_file.read_text(encoding="utf-8")
    assert "password" not in raw_text.lower()
    assert "secret" not in raw_text.lower()
