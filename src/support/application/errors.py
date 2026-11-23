"""Application layer exceptions for database profiles."""


class ProfileError(Exception):
    """Base exception for application-level profile operations."""


class ProfileAlreadyExistsError(ProfileError):
    """Raised when attempting to add or rename a profile to an existing name."""


class ProfileNotFoundError(ProfileError):
    """Raised when a requested profile does not exist."""


class ProfileStorageError(ProfileError):
    """Raised when persistence or storage operations fail."""


class UnsupportedSchemaVersionError(ProfileStorageError):
    """Raised when the database schema version is newer than supported."""


class CredentialError(ProfileError):
    """Base exception for profile credential operations."""


class CredentialStoreUnavailableError(CredentialError):
    """Raised when an approved native secure credential store is not available."""


class CredentialStorageError(CredentialError):
    """Raised when storing a credential fails."""


class CredentialRetrievalError(CredentialError):
    """Raised when retrieving a credential fails."""


class CredentialDeletionError(CredentialError):
    """Raised when deleting a credential fails."""


class ConnectionError(ProfileError):
    """Base exception for database connection failures."""


class ConnectionTimeoutError(ConnectionError):
    """Raised when connecting to the database server times out."""


class HostUnreachableError(ConnectionError):
    """Raised when the host cannot be reached, resolved, or connection was refused."""


class AuthenticationFailedError(ConnectionError):
    """Raised when database authentication fails."""

    def __init__(self, message: str, used_saved_password: bool = False) -> None:
        super().__init__(message)
        self.used_saved_password = used_saved_password


class DatabaseNotFoundError(ConnectionError):
    """Raised when the requested database does not exist on the server."""


class SSLVerificationError(ConnectionError):
    """Raised when SSL/TLS negotiation or certificate verification fails."""


class GeneralConnectionError(ConnectionError):
    """Raised when an unclassified database driver or query failure occurs."""


class UnsupportedDatabaseDriverError(GeneralConnectionError):
    """Raised when attempting connection testing for a database type without an installed connector."""
