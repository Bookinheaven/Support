"""Unit tests for ProfileService application service using an in-memory repository."""

from typing import Optional
from uuid import UUID, uuid4

import pytest

from support.application.database.ports import ProfileRepository
from support.application.database.profile_service import ProfileService
from support.domain.database.errors import (
    DuplicateProfileIdError,
    ProfileAlreadyExistsError,
    ProfileNotFoundError,
    ProfileValidationError,
)
from support.domain.database.profile import DatabaseProfile


class FakeProfileRepository(ProfileRepository):
    """In-memory fake repository implementation for fast, isolated unit tests."""

    def __init__(self) -> None:
        self.profiles: dict[UUID, DatabaseProfile] = {}

    def add(self, profile: DatabaseProfile) -> None:
        self.profiles[profile.id] = profile

    def get_by_id(self, profile_id: UUID) -> Optional[DatabaseProfile]:
        return self.profiles.get(profile_id)

    def get_by_name(self, name: str) -> Optional[DatabaseProfile]:
        target = name.strip().lower()
        for p in self.profiles.values():
            if p.name.lower() == target:
                return p
        return None

    def list_all(self) -> list[DatabaseProfile]:
        return list(self.profiles.values())

    def update(self, profile: DatabaseProfile) -> None:
        self.profiles[profile.id] = profile

    def remove(self, profile_id: UUID) -> None:
        self.profiles.pop(profile_id, None)


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


@pytest.fixture
def service() -> ProfileService:
    repo = FakeProfileRepository()
    return ProfileService(repository=repo)


def test_add_and_list_profiles(service: ProfileService) -> None:
    assert service.list_profiles() == []

    p1 = _sample_profile(name="local-dev")
    p2 = _sample_profile(name="staging")

    service.add_profile(p1)
    service.add_profile(p2)

    profiles = service.list_profiles()
    assert len(profiles) == 2
    assert {p.name for p in profiles} == {"local-dev", "staging"}


def test_add_duplicate_name_rejected_case_insensitive(service: ProfileService) -> None:
    service.add_profile(_sample_profile(name="local-dev"))

    with pytest.raises(ProfileAlreadyExistsError, match="already exists"):
        service.add_profile(_sample_profile(name="local-dev"))

    with pytest.raises(ProfileAlreadyExistsError, match="already exists"):
        service.add_profile(_sample_profile(name="LOCAL-DEV"))


def test_add_duplicate_id_rejected(service: ProfileService) -> None:
    fixed_id = uuid4()
    p1 = _sample_profile(name="profile-1", id=fixed_id)
    p2 = _sample_profile(name="profile-2", id=fixed_id)

    service.add_profile(p1)
    with pytest.raises(DuplicateProfileIdError, match="already exists"):
        service.add_profile(p2)


def test_get_profile_by_name(service: ProfileService) -> None:
    p = _sample_profile(name="staging")
    service.add_profile(p)

    retrieved = service.get_profile_by_name("STAGING")
    assert retrieved.id == p.id
    assert retrieved.name == "staging"

    with pytest.raises(ProfileNotFoundError, match="not found"):
        service.get_profile_by_name("non-existent")


def test_get_profile_by_id(service: ProfileService) -> None:
    p = _sample_profile(name="staging")
    service.add_profile(p)

    retrieved = service.get_profile_by_id(p.id)
    assert retrieved.id == p.id
    assert retrieved.name == "staging"

    with pytest.raises(ProfileNotFoundError, match="not found"):
        service.get_profile_by_id(uuid4())


def test_get_profile_flexible_lookup(service: ProfileService) -> None:
    p = _sample_profile(name="staging")
    service.add_profile(p)

    # Lookup by name
    assert service.get_profile("staging").id == p.id
    # Lookup by UUID string
    assert service.get_profile(str(p.id)).name == "staging"
    # Lookup by UUID object
    assert service.get_profile(p.id).name == "staging"

    with pytest.raises(ProfileNotFoundError, match="not found"):
        service.get_profile("unknown")


def test_remove_profile(service: ProfileService) -> None:
    p = _sample_profile(name="staging")
    service.add_profile(p)
    assert len(service.list_profiles()) == 1

    service.remove_profile("STAGING")
    assert len(service.list_profiles()) == 0

    with pytest.raises(ProfileNotFoundError, match="not found"):
        service.remove_profile("STAGING")


def test_rename_profile_preserves_id(service: ProfileService) -> None:
    p = _sample_profile(name="old-name")
    service.add_profile(p)

    renamed = service.rename_profile("old-name", "new-name")
    assert renamed.id == p.id
    assert renamed.name == "new-name"

    # Old name lookup should fail
    with pytest.raises(ProfileNotFoundError):
        service.get_profile_by_name("old-name")

    # New name lookup should succeed
    assert service.get_profile_by_name("new-name").id == p.id


def test_rename_profile_rejects_existing_name(service: ProfileService) -> None:
    p1 = _sample_profile(name="profile-1")
    p2 = _sample_profile(name="profile-2")
    service.add_profile(p1)
    service.add_profile(p2)

    with pytest.raises(ProfileAlreadyExistsError, match="already exists"):
        service.rename_profile("profile-1", "PROFILE-2")
