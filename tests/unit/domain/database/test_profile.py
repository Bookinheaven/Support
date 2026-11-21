"""Unit tests for DatabaseProfile domain model."""

from dataclasses import replace
from uuid import UUID, uuid4

import pytest

from support.domain.database.errors import ProfileValidationError
from support.domain.database.profile import DatabaseProfile


def _sample_profile(name: str = "local-dev", **kwargs) -> DatabaseProfile:
    """Helper to construct a valid DatabaseProfile with defaults."""
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


def test_create_valid_postgres_profile() -> None:
    profile = DatabaseProfile(
        name="local-dev",
        dialect="postgresql",
        host="localhost",
        port=5432,
        database="myapp",
        username="postgres",
        sslmode="require",
    )
    assert isinstance(profile.id, UUID)
    assert profile.name == "local-dev"
    assert profile.dialect == "postgresql"
    assert profile.host == "localhost"
    assert profile.port == 5432
    assert profile.database == "myapp"
    assert profile.username == "postgres"
    assert profile.sslmode == "require"


def test_automatic_uuid_generation_is_unique() -> None:
    p1 = _sample_profile(name="dev-1")
    p2 = _sample_profile(name="dev-2")
    assert isinstance(p1.id, UUID)
    assert isinstance(p2.id, UUID)
    assert p1.id != p2.id


def test_explicit_uuid_supply() -> None:
    fixed_id = uuid4()
    profile = _sample_profile(name="local-dev", id=fixed_id)
    assert profile.id == fixed_id

    # String UUID should be parsed to UUID object
    str_id = str(uuid4())
    profile_str = _sample_profile(name="staging", id=str_id)
    assert profile_str.id == UUID(str_id)


def test_reject_invalid_uuid() -> None:
    with pytest.raises(ProfileValidationError, match="Invalid profile ID"):
        _sample_profile(id="not-a-valid-uuid")

    with pytest.raises(ProfileValidationError, match="Profile ID must be"):
        _sample_profile(id=12345)  # type: ignore


def test_create_valid_profile_with_aliases() -> None:
    for alias in ["local-dev", "staging", "production-readonly", "pg_db_1"]:
        profile = _sample_profile(name=alias)
        assert profile.name == alias


@pytest.mark.parametrize(
    "invalid_name",
    [
        "",
        "   ",
        "local dev",  # Space
        "local/dev",  # Slash
        "local:dev",  # Colon
        "local@dev",  # At-sign
        "local.dev",  # Dot
        "local\ndev",  # Newline
        "a" * 65,  # Exceeds max length
    ],
)
def test_reject_invalid_profile_names(invalid_name: str) -> None:
    with pytest.raises(ProfileValidationError, match="Profile name"):
        _sample_profile(name=invalid_name)


@pytest.mark.parametrize("invalid_port", [0, -1, 65536, 70000, "5432", True, 5432.0])
def test_reject_invalid_ports(invalid_port) -> None:
    with pytest.raises(ProfileValidationError, match="Port must be"):
        _sample_profile(port=invalid_port)


def test_reject_empty_host() -> None:
    with pytest.raises(ProfileValidationError, match="host cannot be empty"):
        _sample_profile(host="")
    with pytest.raises(ProfileValidationError, match="host cannot be empty"):
        _sample_profile(host="   ")


def test_reject_empty_database() -> None:
    with pytest.raises(ProfileValidationError, match="Database name cannot be empty"):
        _sample_profile(database="")
    with pytest.raises(ProfileValidationError, match="Database name cannot be empty"):
        _sample_profile(database="   ")


def test_reject_empty_username() -> None:
    with pytest.raises(ProfileValidationError, match="Username cannot be empty"):
        _sample_profile(username="")
    with pytest.raises(ProfileValidationError, match="Username cannot be empty"):
        _sample_profile(username="   ")


def test_reject_unsupported_dialect() -> None:
    with pytest.raises(ProfileValidationError, match="Unsupported database dialect 'mysql'"):
        _sample_profile(dialect="mysql")
    with pytest.raises(ProfileValidationError, match="dialect cannot be empty"):
        _sample_profile(dialect="")


def test_reject_invalid_sslmode() -> None:
    with pytest.raises(ProfileValidationError, match="Invalid sslmode 'invalid'"):
        _sample_profile(sslmode="invalid")
    with pytest.raises(ProfileValidationError, match="sslmode must be a non-empty string"):
        _sample_profile(sslmode="   ")


def test_passwords_cannot_be_in_model() -> None:
    with pytest.raises(TypeError, match="unexpected keyword argument 'password'"):
        DatabaseProfile(
            name="local-dev",
            dialect="postgresql",
            host="localhost",
            port=5432,
            database="myapp",
            username="postgres",
            password="secret_password",  # type: ignore
        )

    with pytest.raises(ProfileValidationError, match="sensitive fields: password"):
        DatabaseProfile.from_dict(
            {
                "name": "local-dev",
                "dialect": "postgresql",
                "host": "localhost",
                "port": 5432,
                "database": "myapp",
                "username": "postgres",
                "password": "plain_password",
            }
        )


def test_serialization_and_deserialization_roundtrip() -> None:
    original = _sample_profile(name="staging", sslmode="require")
    data = original.to_dict()

    assert data["id"] == str(original.id)
    assert data["name"] == "staging"
    assert data["dialect"] == "postgresql"
    assert data["sslmode"] == "require"

    restored = DatabaseProfile.from_dict(data)
    assert restored.id == original.id
    assert restored.name == original.name
    assert restored.host == original.host
    assert restored.port == original.port
    assert restored.database == original.database
    assert restored.username == original.username
    assert restored.sslmode == original.sslmode


def test_multiple_profiles_pointing_to_same_database() -> None:
    # Multiple profiles can target the same database if they have different alias names
    p1 = _sample_profile(name="app-dev", host="db.internal", port=5432, database="core_db", username="dev_user")
    p2 = _sample_profile(name="app-ro", host="db.internal", port=5432, database="core_db", username="ro_user")

    assert p1.id != p2.id
    assert p1.host == p2.host
    assert p1.database == p2.database
    assert p1.name != p2.name


def test_renaming_profile_preserves_id() -> None:
    original = _sample_profile(name="old-name")
    renamed = replace(original, name="new-name")

    assert renamed.id == original.id
    assert renamed.name == "new-name"
    assert renamed.host == original.host
    assert renamed.port == original.port
    assert renamed.database == original.database
