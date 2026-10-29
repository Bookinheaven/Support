"""Connection profile persistence manager."""

import json
import os
from pathlib import Path
from typing import Optional
from support.core.exceptions import SupportError
from support.db.models import ConnectionProfile
from support.db.keyring_store import KeyringStore


class ProfileAlreadyExistsError(SupportError):
    """Raised when attempting to add a profile with an existing name."""

    def __init__(self, name: str) -> None:
        super().__init__(
            f"Profile '{name}' already exists",
            details="Use a different name or specify overwrite/force to replace it.",
        )
        self.profile_name = name


class ProfileNotFoundError(SupportError):
    """Raised when a requested profile is not found."""

    def __init__(self, name: str) -> None:
        super().__init__(
            f"Connection profile '{name}' not found",
            details="Use 'support db list' to view available connection profiles.",
        )
        self.profile_name = name


class ProfileManager:
    """Manages saving, loading, listing, and removing database connection profiles."""

    def __init__(self, config_dir: Optional[Path] = None) -> None:
        if config_dir is not None:
            self.config_dir = Path(config_dir)
        else:
            env_dir = os.getenv("SUPPORT_CONFIG_DIR")
            self.config_dir = Path(env_dir) if env_dir else Path.home() / ".support"

        self.profiles_file = self.config_dir / "profiles.json"
        self._ensure_storage_exists()

    def _ensure_storage_exists(self) -> None:
        """Create configuration directory and initialize empty profiles file if missing."""
        try:
            self.config_dir.mkdir(parents=True, exist_ok=True)
            if not self.profiles_file.exists():
                self.profiles_file.write_text("{}", encoding="utf-8")
        except Exception as e:
            raise SupportError(
                f"Failed to initialize profile storage in '{self.config_dir}'",
                details=str(e),
            )

    def _read_raw_profiles(self) -> dict[str, dict]:
        """Read profile dictionary from disk."""
        try:
            content = self.profiles_file.read_text(encoding="utf-8").strip()
            if not content:
                return {}
            return json.loads(content)
        except Exception:
            return {}

    def _write_raw_profiles(self, data: dict[str, dict]) -> None:
        """Write profile dictionary to disk."""
        self.profiles_file.write_text(json.dumps(data, indent=2), encoding="utf-8")

    def list_profiles(self) -> list[ConnectionProfile]:
        """List all saved connection profiles (sorted by name)."""
        raw = self._read_raw_profiles()
        profiles = [ConnectionProfile.model_validate(p) for p in raw.values()]
        return sorted(profiles, key=lambda p: p.name.lower())

    def get_profile(self, name: str) -> Optional[ConnectionProfile]:
        """Lookup a profile by name."""
        raw = self._read_raw_profiles()
        entry = raw.get(name)
        if not entry:
            # Case-insensitive search fallback
            target = name.lower()
            for key, val in raw.items():
                if key.lower() == target:
                    return ConnectionProfile.model_validate(val)
            return None
        return ConnectionProfile.model_validate(entry)

    def save_profile(
        self,
        profile: ConnectionProfile,
        password: Optional[str] = None,
        overwrite: bool = False,
    ) -> None:
        """Save a connection profile and optionally store its password in the keyring."""
        raw = self._read_raw_profiles()
        existing = self.get_profile(profile.name)

        if existing and not overwrite:
            raise ProfileAlreadyExistsError(profile.name)

        # Store non-secret metadata
        raw[profile.name] = profile.model_dump(mode="json")
        self._write_raw_profiles(raw)

        # Store password if provided
        if password is not None:
            KeyringStore.set_password(profile.name, password)

    def delete_profile(self, name: str) -> bool:
        """Delete a profile and its associated keyring credential."""
        profile = self.get_profile(name)
        if not profile:
            raise ProfileNotFoundError(name)

        raw = self._read_raw_profiles()
        if profile.name in raw:
            del raw[profile.name]
            self._write_raw_profiles(raw)

        # Remove password from keyring
        KeyringStore.delete_password(profile.name)
        return True

    def get_profile_with_password(self, name: str) -> tuple[ConnectionProfile, Optional[str]]:
        """Retrieve profile and its secret password."""
        profile = self.get_profile(name)
        if not profile:
            raise ProfileNotFoundError(name)

        password = KeyringStore.get_password(profile.name)
        return profile, password
