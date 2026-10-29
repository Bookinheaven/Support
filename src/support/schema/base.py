"""Base interfaces and result data structures for schema sources."""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional, Literal
from support.schema.models import DatabaseSchema


@dataclass
class DiscoveryWarning:
    """Warning or conflict encountered during schema discovery."""

    message: str
    file_path: Optional[str] = None
    severity: Literal["warning", "conflict", "unsupported", "info"] = "warning"


@dataclass
class SchemaDiscoveryResult:
    """Consolidated outcome of scanning and discovering database schemas."""

    schema: DatabaseSchema
    discovered_files: list[Path] = field(default_factory=list)
    parsed_files: list[Path] = field(default_factory=list)
    failed_files: dict[str, str] = field(default_factory=dict)
    warnings: list[DiscoveryWarning] = field(default_factory=list)

    @property
    def has_errors(self) -> bool:
        """Whether any discovery errors occurred."""
        return len(self.failed_files) > 0

    @property
    def is_empty(self) -> bool:
        """Whether no tables were discovered."""
        return len(self.schema.tables) == 0

    def add_warning(
        self,
        message: str,
        file_path: Optional[str] = None,
        severity: Literal["warning", "conflict", "unsupported", "info"] = "warning",
    ) -> None:
        """Record a discovery warning."""
        self.warnings.append(
            DiscoveryWarning(message=message, file_path=file_path, severity=severity)
        )


class BaseSchemaSource(ABC):
    """Abstract interface for all schema discovery sources (SQL files, SQLite, etc.)."""

    @abstractmethod
    def discover(self) -> SchemaDiscoveryResult:
        """Discover and return database schema."""
        pass
