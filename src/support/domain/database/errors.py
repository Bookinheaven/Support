"""Domain errors for database profiles."""


class ProfileError(Exception):
    """Base exception for all profile-related errors."""


class ProfileValidationError(ProfileError, ValueError):
    """Raised when profile validation fails."""


class ProfileNotFoundError(ProfileError, KeyError):
    """Raised when a requested profile does not exist."""


class ProfileAlreadyExistsError(ProfileError, ValueError):
    """Raised when attempting to add a profile with an existing name."""


class DuplicateProfileIdError(ProfileError, ValueError):
    """Raised when a profile ID conflict is detected."""
