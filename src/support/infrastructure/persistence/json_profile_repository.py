"""JSON-backed profile repository implementation with migration support."""

import json
import os
from pathlib import Path
import tempfile
from typing import Optional
from uuid import UUID, uuid4

from support.application.database.ports import ProfileRepository
from support.domain.database.errors import (
    DuplicateProfileIdError,
    ProfileAlreadyExistsError,
    ProfileError,
    ProfileNotFoundError,
    ProfileValidationError,
)
from support.domain.database.profile import DatabaseProfile


class StorageError(ProfileError):
    """Base exception for profile storage persistence errors."""


class StorageCorruptedError(StorageError):
    """Raised when the profile storage file is corrupted or contains invalid JSON/structure."""


class StorageVersionError(StorageError):
    """Raised when the profile storage file has an unsupported schema version."""


DEFAULT_PROFILES_PATH = Path.home() / ".support" / "profiles.json"
CURRENT_STORAGE_VERSION = 2


class JsonProfileRepository(ProfileRepository):
    """Persists DatabaseProfile objects in a versioned JSON file.

    Supports atomic writes, restrictive permissions, and seamless migration
    from schema version 1 to schema version 2.
    """

    def __init__(self, path: Optional[Path | str] = None) -> None:
        if path is None:
            self.path = DEFAULT_PROFILES_PATH
        else:
            self.path = Path(path).expanduser().resolve()

    def add(self, profile: DatabaseProfile) -> None:
        """Add a profile to storage.

        Raises:
            DuplicateProfileIdError: If a profile with the same ID already exists.
            ProfileAlreadyExistsError: If a profile with the same name already exists.
        """
        profiles = self._load()
        if profile.id in profiles:
            raise DuplicateProfileIdError(
                f"Database profile with ID '{profile.id}' already exists."
            )
        target_name = profile.name.lower()
        for existing in profiles.values():
            if existing.name.lower() == target_name:
                raise ProfileAlreadyExistsError(
                    f"Database profile '{profile.name}' already exists (names are case-insensitive)."
                )

        profiles[profile.id] = profile
        self._save(list(profiles.values()))

    def get_by_id(self, profile_id: UUID) -> Optional[DatabaseProfile]:
        """Retrieve a profile by UUID."""
        profiles = self._load()
        return profiles.get(profile_id)

    def get_by_name(self, name: str) -> Optional[DatabaseProfile]:
        """Retrieve a profile by case-insensitive name."""
        target = name.strip().lower()
        for profile in self._load().values():
            if profile.name.lower() == target:
                return profile
        return None

    def list_all(self) -> list[DatabaseProfile]:
        """Return all profiles in storage."""
        return list(self._load().values())

    def update(self, profile: DatabaseProfile) -> None:
        """Update an existing profile by ID.

        Raises:
            ProfileNotFoundError: If profile does not exist.
            ProfileAlreadyExistsError: If the new name conflicts with another profile.
        """
        profiles = self._load()
        if profile.id not in profiles:
            raise ProfileNotFoundError(
                f"Database profile with ID '{profile.id}' not found."
            )

        target_name = profile.name.lower()
        for existing_id, existing in profiles.items():
            if existing_id != profile.id and existing.name.lower() == target_name:
                raise ProfileAlreadyExistsError(
                    f"Database profile '{profile.name}' already exists (names are case-insensitive)."
                )

        profiles[profile.id] = profile
        self._save(list(profiles.values()))

    def remove(self, profile_id: UUID) -> None:
        """Remove a profile by ID.

        Raises:
            ProfileNotFoundError: If profile does not exist.
        """
        profiles = self._load()
        if profile_id not in profiles:
            raise ProfileNotFoundError(
                f"Database profile with ID '{profile_id}' not found."
            )

        del profiles[profile_id]
        self._save(list(profiles.values()))

    def _load(self) -> dict[UUID, DatabaseProfile]:
        """Load, validate, and if needed migrate profiles from JSON file."""
        if not self.path.exists():
            return {}

        try:
            content = self.path.read_text(encoding="utf-8")
        except OSError as err:
            raise StorageError(f"Failed to read profiles file at {self.path}: {err}") from err

        if not content.strip():
            raise StorageCorruptedError(f"Profiles file at {self.path} is empty.")

        try:
            raw_data = json.loads(content)
        except json.JSONDecodeError as err:
            raise StorageCorruptedError(
                f"Malformed JSON in profiles file at {self.path}: {err}"
            ) from err

        if not isinstance(raw_data, dict):
            raise StorageCorruptedError(
                f"Invalid structure in profiles file at {self.path}: root must be a JSON object."
            )

        if "version" not in raw_data:
            raise StorageCorruptedError(
                f"Missing 'version' field in profiles file at {self.path}."
            )

        version = raw_data["version"]
        if version == 1:
            return self._migrate_v1_to_v2(raw_data)
        elif version == CURRENT_STORAGE_VERSION:
            return self._load_v2(raw_data)
        else:
            raise StorageVersionError(
                f"Unsupported configuration version '{version}' in {self.path}. "
                f"Expected version {CURRENT_STORAGE_VERSION}."
            )

    def _migrate_v1_to_v2(self, raw_data: dict) -> dict[UUID, DatabaseProfile]:
        """Migrate version 1 profile configuration to version 2 by assigning stable UUIDs."""
        if "profiles" not in raw_data or not isinstance(raw_data["profiles"], list):
            raise StorageCorruptedError(
                f"Missing or invalid 'profiles' list in v1 profiles file at {self.path}."
            )

        profiles: dict[UUID, DatabaseProfile] = {}
        seen_names: set[str] = set()

        for idx, item in enumerate(raw_data["profiles"]):
            if not isinstance(item, dict):
                raise StorageCorruptedError(
                    f"Invalid profile entry at index {idx} in {self.path}: must be an object."
                )
            try:
                # Generate new UUID for v1 profile lacking ID
                prof_id = uuid4()
                prof_data = dict(item)
                prof_data["id"] = prof_id
                prof = DatabaseProfile.from_dict(prof_data)
            except ProfileValidationError as val_err:
                raise StorageCorruptedError(
                    f"Corrupted v1 profile entry at index {idx} in {self.path}: {val_err}"
                ) from val_err

            name_lower = prof.name.lower()
            if name_lower in seen_names:
                raise StorageCorruptedError(
                    f"Duplicate profile name '{prof.name}' detected in v1 config {self.path}."
                )
            seen_names.add(name_lower)
            profiles[prof.id] = prof

        # Persist migrated v2 file atomically
        self._save(list(profiles.values()))
        return profiles

    def _load_v2(self, raw_data: dict) -> dict[UUID, DatabaseProfile]:
        """Load and validate version 2 profile configuration."""
        if "profiles" not in raw_data or not isinstance(raw_data["profiles"], list):
            raise StorageCorruptedError(
                f"Missing or invalid 'profiles' list in profiles file at {self.path}."
            )

        profiles: dict[UUID, DatabaseProfile] = {}
        seen_names: set[str] = set()

        for idx, item in enumerate(raw_data["profiles"]):
            if not isinstance(item, dict):
                raise StorageCorruptedError(
                    f"Invalid profile entry at index {idx} in {self.path}: must be an object."
                )
            if "id" not in item or not item["id"]:
                raise StorageCorruptedError(
                    f"Missing 'id' field in v2 profile at index {idx} in {self.path}."
                )
            try:
                prof = DatabaseProfile.from_dict(item)
            except ProfileValidationError as val_err:
                raise StorageCorruptedError(
                    f"Corrupted profile entry at index {idx} in {self.path}: {val_err}"
                ) from val_err

            if prof.id in profiles:
                raise StorageCorruptedError(
                    f"Duplicate profile ID '{prof.id}' detected in {self.path}."
                )

            name_lower = prof.name.lower()
            if name_lower in seen_names:
                raise StorageCorruptedError(
                    f"Duplicate profile name '{prof.name}' detected in {self.path}."
                )

            seen_names.add(name_lower)
            profiles[prof.id] = prof

        return profiles

    def _save(self, profiles: list[DatabaseProfile]) -> None:
        """Atomically persist profiles list in version 2 format with restrictive permissions."""
        data = {
            "version": CURRENT_STORAGE_VERSION,
            "profiles": [p.to_dict() for p in profiles],
        }

        parent_dir = self.path.parent
        if not parent_dir.exists():
            try:
                parent_dir.mkdir(parents=True, exist_ok=True)
                parent_dir.chmod(0o700)
            except OSError as err:
                raise StorageError(
                    f"Failed to create configuration directory {parent_dir}: {err}"
                ) from err

        try:
            parent_dir.chmod(0o700)
        except OSError:
            pass

        try:
            serialized = json.dumps(data, indent=2) + "\n"
        except Exception as err:
            raise StorageError(f"Failed to serialize profiles: {err}") from err

        tmp_path: Optional[Path] = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w",
                dir=parent_dir,
                delete=False,
                encoding="utf-8",
                prefix=".profiles_tmp_",
            ) as tmp_file:
                tmp_path = Path(tmp_file.name)
                tmp_file.write(serialized)
                tmp_file.flush()
                os.fsync(tmp_file.fileno())

            try:
                tmp_path.chmod(0o600)
            except OSError:
                pass

            tmp_path.replace(self.path)
            tmp_path = None
        except OSError as err:
            raise StorageError(
                f"Failed to write profiles atomically to {self.path}: {err}"
            ) from err
        finally:
            if tmp_path is not None and tmp_path.exists():
                try:
                    tmp_path.unlink()
                except OSError:
                    pass
