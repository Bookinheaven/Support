"""DatabaseProfile domain model."""

from dataclasses import dataclass, field
import re
from typing import Any, Optional
from uuid import UUID, uuid4

from support.domain.database.errors import ProfileValidationError

VALID_DIALECTS = {"postgresql", "postgres"}
VALID_SSL_MODES = {"disable", "allow", "prefer", "require", "verify-ca", "verify-full"}
NAME_PATTERN = re.compile(r"^[a-zA-Z0-9_-]+$")


@dataclass(frozen=True)
class DatabaseProfile:
    """Represents a named, persistent PostgreSQL database connection profile.

    Attributes:
        name: Unique, human-readable profile alias (case-insensitive uniqueness).
        dialect: Database engine dialect (currently postgresql).
        host: Database host name or IP address.
        port: Database port number (1-65535).
        database: Database name.
        username: Database user name.
        id: Stable unique identifier (UUID v4). Generated automatically if omitted.
        sslmode: Optional SSL mode (e.g. disable, require, verify-full).

    Passwords and secrets are explicitly prohibited from this model.
    """

    name: str
    dialect: str
    host: str
    port: int
    database: str
    username: str
    id: UUID = field(default_factory=uuid4)
    sslmode: Optional[str] = None

    def __post_init__(self) -> None:
        # 1. ID validation
        if isinstance(self.id, str):
            try:
                parsed_id = UUID(self.id)
            except (ValueError, AttributeError) as exc:
                raise ProfileValidationError(
                    f"Invalid profile ID '{self.id}': must be a valid UUID."
                ) from exc
            object.__setattr__(self, "id", parsed_id)
        elif not isinstance(self.id, UUID):
            raise ProfileValidationError(
                f"Profile ID must be a UUID instance or valid UUID string, got {type(self.id).__name__}."
            )

        # 2. Name validation
        if not isinstance(self.name, str) or not self.name.strip():
            raise ProfileValidationError("Profile name cannot be empty.")
        if not NAME_PATTERN.match(self.name):
            raise ProfileValidationError(
                f"Invalid profile name '{self.name}'. "
                "Profile names must contain only alphanumeric characters, underscores, or hyphens."
            )
        if len(self.name) > 64:
            raise ProfileValidationError(
                f"Profile name '{self.name}' exceeds maximum allowed length of 64 characters."
            )

        # 3. Dialect validation (PostgreSQL only currently)
        if not isinstance(self.dialect, str) or not self.dialect.strip():
            raise ProfileValidationError("Database dialect cannot be empty.")
        normalized_dialect = self.dialect.strip().lower()
        if normalized_dialect not in VALID_DIALECTS:
            raise ProfileValidationError(
                f"Unsupported database dialect '{self.dialect}'. Only 'postgresql' is currently supported."
            )
        object.__setattr__(self, "dialect", "postgresql")

        # 4. Host validation
        if not isinstance(self.host, str) or not self.host.strip():
            raise ProfileValidationError("Database host cannot be empty.")
        object.__setattr__(self, "host", self.host.strip())

        # 5. Port validation
        if isinstance(self.port, bool) or not isinstance(self.port, int):
            raise ProfileValidationError(f"Port must be an integer, got {type(self.port).__name__}.")
        if not (1 <= self.port <= 65535):
            raise ProfileValidationError(f"Port must be between 1 and 65535, got {self.port}.")

        # 6. Database name validation
        if not isinstance(self.database, str) or not self.database.strip():
            raise ProfileValidationError("Database name cannot be empty.")
        object.__setattr__(self, "database", self.database.strip())

        # 7. Username validation
        if not isinstance(self.username, str) or not self.username.strip():
            raise ProfileValidationError("Username cannot be empty.")
        object.__setattr__(self, "username", self.username.strip())

        # 8. SSL mode validation
        if self.sslmode is not None:
            if not isinstance(self.sslmode, str) or not self.sslmode.strip():
                raise ProfileValidationError("sslmode must be a non-empty string or None.")
            normalized_ssl = self.sslmode.strip().lower()
            if normalized_ssl not in VALID_SSL_MODES:
                raise ProfileValidationError(
                    f"Invalid sslmode '{self.sslmode}'. Must be one of: {', '.join(sorted(VALID_SSL_MODES))}."
                )
            object.__setattr__(self, "sslmode", normalized_ssl)

    def to_dict(self) -> dict[str, Any]:
        """Convert profile to serializable dictionary for storage."""
        return {
            "id": str(self.id),
            "name": self.name,
            "dialect": self.dialect,
            "host": self.host,
            "port": self.port,
            "database": self.database,
            "username": self.username,
            "sslmode": self.sslmode,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "DatabaseProfile":
        """Instantiate profile from dictionary, strictly rejecting sensitive fields."""
        if not isinstance(data, dict):
            raise ProfileValidationError("Profile data must be a dictionary.")

        forbidden = {"password", "secret", "credential", "key", "token"}
        found_forbidden = forbidden.intersection(k.lower() for k in data.keys())
        if found_forbidden:
            raise ProfileValidationError(
                f"Profile data must not contain sensitive fields: {', '.join(sorted(found_forbidden))}."
            )

        required = ("name", "dialect", "host", "port", "database", "username")
        missing = [f for f in required if f not in data]
        if missing:
            raise ProfileValidationError(f"Missing required profile fields: {', '.join(missing)}.")

        raw_id = data.get("id")
        kwargs: dict[str, Any] = {
            "name": data["name"],
            "dialect": data["dialect"],
            "host": data["host"],
            "port": data["port"],
            "database": data["database"],
            "username": data["username"],
            "sslmode": data.get("sslmode"),
        }
        if raw_id is not None:
            kwargs["id"] = raw_id

        return cls(**kwargs)
