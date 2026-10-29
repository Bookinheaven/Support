"""Domain exceptions for Support."""

from pathlib import Path
from typing import Optional


class SupportError(Exception):
    """Base exception for all Support errors."""

    def __init__(self, message: str, details: Optional[str] = None) -> None:
        super().__init__(message)
        self.message = message
        self.details = details

    def __str__(self) -> str:
        if self.details:
            return f"{self.message}: {self.details}"
        return self.message


class InvalidPathError(SupportError):
    """Raised when a provided path does not exist or has an invalid type."""

    def __init__(self, path: Path | str, reason: str = "Path does not exist") -> None:
        super().__init__(f"Invalid path '{path}'", details=reason)
        self.path = Path(path)


class UnreadableFileError(SupportError):
    """Raised when a file cannot be read due to permissions or encoding."""

    def __init__(self, path: Path | str, reason: str) -> None:
        super().__init__(f"Cannot read file '{path}'", details=reason)
        self.path = Path(path)


class SQLParseError(SupportError):
    """Raised when SQL parsing fails completely."""

    def __init__(self, file_path: Optional[Path | str], error: str) -> None:
        loc = f" in '{file_path}'" if file_path else ""
        super().__init__(f"Failed to parse SQL{loc}", details=error)
        self.file_path = Path(file_path) if file_path else None


class UnsupportedDialectError(SupportError):
    """Raised when an unknown or unsupported database dialect is specified."""

    def __init__(self, dialect: str) -> None:
        super().__init__(
            f"Unsupported dialect '{dialect}'",
            details="Supported dialects are postgres, mysql, sqlite, mssql",
        )
        self.dialect = dialect


class EmptySchemaError(SupportError):
    """Raised when no database schema elements could be discovered."""

    def __init__(self, source_path: Path | str) -> None:
        super().__init__(
            f"No schema discovered in '{source_path}'",
            details="No valid CREATE TABLE statements or schema metadata found.",
        )
        self.source_path = Path(source_path)


class DatabaseAccessError(SupportError):
    """Raised when a database (e.g. SQLite, PostgreSQL, MySQL, MSSQL) cannot be accessed."""

    def __init__(self, path: Path | str, reason: str, details: Optional[str] = None) -> None:
        detail_str = f"{reason}\nHint: {details}" if details else reason
        super().__init__(f"Database access failure for '{path}'", details=details or reason)
        self.path = path


class LLMError(SupportError):
    """Base class for all LLM gateway errors."""


class ProviderNotConfiguredError(LLMError):
    """Raised when a requested provider is missing required configuration (e.g. API key)."""

    def __init__(self, provider: str, hint: str) -> None:
        super().__init__(f"Provider '{provider}' is not configured", details=hint)
        self.provider = provider


class ProviderUnreachableError(LLMError):
    """Raised when an LLM provider endpoint cannot be reached."""

    def __init__(self, provider: str, reason: str, hint: Optional[str] = None) -> None:
        super().__init__(f"Provider '{provider}' is unreachable: {reason}", details=hint)
        self.provider = provider


class ModelNotFoundError(LLMError):
    """Raised when a requested model is not found or not installed."""

    def __init__(self, provider: str, model: str, hint: Optional[str] = None) -> None:
        super().__init__(f"Model '{model}' not found on provider '{provider}'", details=hint)
        self.provider = provider
        self.model = model


class LLMAuthenticationError(LLMError):
    """Raised when authentication fails for an LLM provider."""

    def __init__(self, provider: str, reason: str, hint: Optional[str] = None) -> None:
        super().__init__(f"Authentication failed for provider '{provider}': {reason}", details=hint)
        self.provider = provider


class GenerationError(LLMError):
    """Raised when prompt generation fails."""

    def __init__(self, provider: str, model: str, reason: str, details: Optional[str] = None) -> None:
        super().__init__(f"Generation failed on '{provider}/{model}': {reason}", details=details)
        self.provider = provider
        self.model = model


class KeyringUnavailableError(SupportError):
    """Raised when no secure OS keyring backend is operational."""

    def __init__(self, details: Optional[str] = None) -> None:
        super().__init__(
            "No secure OS keyring backend available",
            details=details or "Ensure an OS credential manager is available, or use environment variables as a fallback.",
        )
