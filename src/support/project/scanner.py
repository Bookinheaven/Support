"""Project directory scanning and schema file detection."""

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from support.core.config import AppConfig
from support.core.exceptions import InvalidPathError


@dataclass
class DiscoveredProjectSources:
    """Artifacts discovered during a project directory scan."""

    root_dir: Path
    sql_files: list[Path] = field(default_factory=list)
    sqlite_files: list[Path] = field(default_factory=list)
    oversized_files: list[Path] = field(default_factory=list)
    skipped_directories: list[str] = field(default_factory=list)

    @property
    def total_schema_sources(self) -> int:
        """Total count of potential schema sources found."""
        return len(self.sql_files) + len(self.sqlite_files)


class ProjectScanner:
    """Scans project trees for SQL DDL files, migration files, and SQLite databases."""

    def __init__(self, config: Optional[AppConfig] = None) -> None:
        self.config = config or AppConfig.from_env()

    def scan(self, directory_path: Path | str) -> DiscoveredProjectSources:
        """Scan a project directory for recognized database schema files.

        Prunes common irrelevant directories and avoids arbitrarily large files.
        """
        root = Path(directory_path).resolve()
        if not root.exists():
            raise InvalidPathError(root, "Target directory does not exist")
        if not root.is_dir():
            raise InvalidPathError(root, "Target path is not a directory")

        results = DiscoveredProjectSources(root_dir=root)
        ignored_names = set(self.config.ignored_directories)

        for dirpath, dirnames, filenames in os.walk(root):
            # Prune ignored directories in-place
            dirnames[:] = [d for d in dirnames if d not in ignored_names and not d.startswith(".")]

            current_dir = Path(dirpath)

            for filename in filenames:
                file_path = current_dir / filename
                suffix = file_path.suffix.lower()

                # Check SQL files
                if suffix in (".sql", ".ddl"):
                    try:
                        size = file_path.stat().st_size
                        if size > self.config.max_file_size_bytes:
                            results.oversized_files.append(file_path)
                        else:
                            results.sql_files.append(file_path)
                    except OSError:
                        continue

                # Check SQLite files
                elif suffix in (".sqlite", ".sqlite3", ".db"):
                    if self._is_sqlite_file(file_path):
                        results.sqlite_files.append(file_path)

        # Sort files deterministically
        results.sql_files.sort()
        results.sqlite_files.sort()
        results.oversized_files.sort()

        return results

    def _is_sqlite_file(self, file_path: Path) -> bool:
        """Check if file starts with the SQLite 3 magic header bytes."""
        try:
            with open(file_path, "rb") as f:
                header = f.read(16)
                return header.startswith(b"SQLite format 3\x00")
        except Exception:
            return False
