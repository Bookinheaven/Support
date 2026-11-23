"""Application layer exceptions for database profiles, credentials, and connectivity."""


class SupportError(Exception):
    """Base exception for all application-level Support errors."""


class ProfileError(SupportError):
    """Base exception for application-level profile operations."""


class ProfileAlreadyExistsError(ProfileError):
    """Raised when attempting to add or rename a profile to an existing name."""


class ProfileNotFoundError(ProfileError):
    """Raised when a requested profile does not exist."""


class ProfileStorageError(ProfileError):
    """Raised when persistence or storage operations fail."""


class UnsupportedSchemaVersionError(ProfileStorageError):
    """Raised when the database schema version is newer than supported."""


class CredentialError(SupportError):
    """Base exception for profile credential operations."""


class CredentialStoreUnavailableError(CredentialError):
    """Raised when an approved native secure credential store is not available."""


class CredentialStorageError(CredentialError):
    """Raised when storing a credential fails."""


class CredentialRetrievalError(CredentialError):
    """Raised when retrieving a credential fails."""


class CredentialDeletionError(CredentialError):
    """Raised when deleting a credential fails."""


class DatabaseConnectionError(SupportError):
    """Base exception for database connection failures."""


class ConnectionTimeoutError(DatabaseConnectionError):
    """Raised when connecting to the database server times out."""


class HostUnreachableError(DatabaseConnectionError):
    """Raised when the host cannot be reached, resolved, or connection was refused."""


class AuthenticationFailedError(DatabaseConnectionError):
    """Raised when database authentication fails."""

    def __init__(self, message: str, used_saved_password: bool = False) -> None:
        super().__init__(message)
        self.used_saved_password = used_saved_password


class DatabaseNotFoundError(DatabaseConnectionError):
    """Raised when the requested database does not exist on the server."""


class SSLVerificationError(DatabaseConnectionError):
    """Raised when SSL/TLS negotiation or certificate verification fails."""


class GeneralConnectionError(DatabaseConnectionError):
    """Raised when an unclassified database driver or query failure occurs."""


class UnsupportedDatabaseDriverError(GeneralConnectionError):
    """Raised when attempting connection testing for a database type without an installed connector."""


class SchemaIntrospectionError(SupportError):
    """Base exception for database schema introspection failures."""


class ObjectNotFoundError(SchemaIntrospectionError):
    """Raised when a requested table, view, or collection is not found in the database schema."""


class AmbiguousObjectNameError(SchemaIntrospectionError):
    """Raised when an unqualified object name matches multiple objects in different schemas."""


class AssistantError(SupportError):
    """Base exception for conversational assistant errors."""


class ToolExecutionError(AssistantError):
    """Raised when execution of a tool fails."""


class InvalidToolArgumentsError(ToolExecutionError):
    """Raised when arguments provided for a tool do not conform to schema."""


class UnknownToolError(ToolExecutionError):
    """Raised when an unregistered tool is requested."""


class OllamaError(SupportError):
    """Base exception for Ollama model and server operations."""


class OllamaConnectionError(OllamaError):
    """Raised when the Ollama server cannot be reached."""


class OllamaTimeoutError(OllamaError):
    """Raised when communication with the Ollama server times out."""


class OllamaModelNotFoundError(OllamaError):
    """Raised when the requested model is not installed locally in Ollama."""


class OllamaToolCallingUnsupportedError(OllamaError):
    """Raised when the selected model does not support native tool calling."""


class OllamaResponseError(OllamaError):
    """Raised when the Ollama server returns an unexpected or invalid response."""
