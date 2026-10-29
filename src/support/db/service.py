"""Database profile service layer decoupling business logic from CLI commands."""

import os
from typing import Optional, Any
from support.core.dialect import Dialect
from support.core.exceptions import SupportError
from support.db.models import ConnectionProfile
from support.db.manager import ProfileManager, ProfileAlreadyExistsError, ProfileNotFoundError
from support.db.keyring_store import KeyringStore
from support.db.url_parser import parse_database_url
from support.db.health import (
    HealthCheckResult,
    check_connection_health,
    check_profiles_concurrently,
)


class ConnectionVerificationError(SupportError):
    """Raised when database connectivity validation fails during registration or test."""

    def __init__(self, profile: ConnectionProfile, result: HealthCheckResult) -> None:
        super().__init__(
            f"Connection failed · {result.status}",
            details=result.error_message,
        )
        self.profile = profile
        self.result = result


class DatabaseService:
    """Core service for database registration, connection health checks, and profile management."""

    def __init__(self, manager: Optional[ProfileManager] = None) -> None:
        self.manager = manager or ProfileManager()

    def register_profile(
        self,
        url: Optional[str] = None,
        name: Optional[str] = None,
        dialect: Optional[str] = None,
        host: Optional[str] = None,
        port: Optional[int] = None,
        database: Optional[str] = None,
        username: Optional[str] = None,
        password: Optional[str] = None,
        password_env: Optional[str] = None,
        options: Optional[dict[str, Any]] = None,
        overwrite: bool = False,
        test_connection: bool = True,
        test_timeout: float = 5.0,
    ) -> tuple[ConnectionProfile, Optional[HealthCheckResult]]:
        """Normalize, validate, verify, and persist a database connection profile.

        Raises:
            ConnectionVerificationError: if test_connection is True and connection check fails.
            ProfileAlreadyExistsError: if explicit name exists and overwrite is False.
            SupportError: on invalid inputs or malformed URLs.
        """
        existing_profiles = self.manager.list_profiles()
        existing_names = {p.name for p in existing_profiles}

        effective_password: Optional[str] = None
        profile: Optional[ConnectionProfile] = None

        # 1. Parse via URL
        if url:
            profile, url_pw = parse_database_url(
                url_string=url,
                name_override=name,
                existing_names=existing_names if not name else None,
            )
            if password_env:
                effective_password = os.getenv(password_env)
            elif password is not None:
                effective_password = password
            else:
                effective_password = url_pw

            if name and name in existing_names and not overwrite:
                raise ProfileAlreadyExistsError(name)

        # 2. Parse via explicit parameters
        else:
            if not name:
                raise SupportError("Profile name is required.")
            if name in existing_names and not overwrite:
                raise ProfileAlreadyExistsError(name)

            parsed_dialect = Dialect.from_string(dialect)
            if parsed_dialect == Dialect.UNKNOWN:
                raise SupportError(f"Unsupported database dialect '{dialect}'.")

            if password_env:
                effective_password = os.getenv(password_env)
            else:
                effective_password = password

            profile = ConnectionProfile(
                name=name,
                dialect=parsed_dialect,
                host=host,
                port=port,
                database=database or "",
                username=username,
                options=options or {},
            )

        # 3. Connection verification (if requested)
        health_result: Optional[HealthCheckResult] = None
        if test_connection:
            health_result = check_connection_health(
                profile, password=effective_password, timeout=test_timeout
            )
            if not health_result.is_online:
                raise ConnectionVerificationError(profile, health_result)

        # 4. Save profile & credential only after successful verification
        self.manager.save_profile(profile, password=effective_password, overwrite=overwrite)
        return profile, health_result

    def list_profiles_with_status(
        self,
        check_health: bool = True,
        timeout: float = 3.0,
    ) -> tuple[list[ConnectionProfile], dict[str, HealthCheckResult]]:
        """Retrieve all saved profiles and optionally probe their live health concurrently."""
        profiles = self.manager.list_profiles()
        if not check_health or not profiles:
            return profiles, {}

        passwords = {p.name: KeyringStore.get_password(p.name) for p in profiles}
        health_results = check_profiles_concurrently(profiles, passwords, timeout=timeout)
        return profiles, health_results

    def test_profile(self, name: str, timeout: float = 5.0) -> tuple[ConnectionProfile, HealthCheckResult]:
        """Test connectivity of an existing saved profile."""
        profile, password = self.manager.get_profile_with_password(name)
        result = check_connection_health(profile, password=password, timeout=timeout)
        return profile, result

    def remove_profile(self, name: str) -> bool:
        """Remove a saved connection profile and its stored credentials."""
        return self.manager.delete_profile(name)
