"""Application layer for Support."""

from support.application.connection import ConnectionTestWorkflow
from support.application.errors import (
    AuthenticationFailedError,
    ConnectionError,
    ConnectionTimeoutError,
    CredentialDeletionError,
    CredentialError,
    CredentialRetrievalError,
    CredentialStorageError,
    CredentialStoreUnavailableError,
    DatabaseNotFoundError,
    GeneralConnectionError,
    HostUnreachableError,
    ProfileAlreadyExistsError,
    ProfileError,
    ProfileNotFoundError,
    ProfileStorageError,
    SSLVerificationError,
    UnsupportedDatabaseDriverError,
    UnsupportedSchemaVersionError,
)
from support.application.ports import (
    ConnectionTestResult,
    CredentialStore,
    DatabaseConnector,
    ProfileRepository,
)
from support.application.service import ProfileService

__all__ = [
    "AuthenticationFailedError",
    "ConnectionError",
    "ConnectionTestResult",
    "ConnectionTestWorkflow",
    "ConnectionTimeoutError",
    "CredentialDeletionError",
    "CredentialError",
    "CredentialRetrievalError",
    "CredentialStorageError",
    "CredentialStore",
    "CredentialStoreUnavailableError",
    "DatabaseConnector",
    "DatabaseNotFoundError",
    "GeneralConnectionError",
    "HostUnreachableError",
    "ProfileAlreadyExistsError",
    "ProfileError",
    "ProfileNotFoundError",
    "ProfileRepository",
    "ProfileService",
    "ProfileStorageError",
    "SSLVerificationError",
    "UnsupportedDatabaseDriverError",
    "UnsupportedSchemaVersionError",
]
