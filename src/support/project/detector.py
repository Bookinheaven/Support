"""Dialect detection and framework clue analysis."""

from pathlib import Path
from support.core.dialect import Dialect
from support.schema.sql_parser import detect_dialect_from_sql
from support.project.scanner import DiscoveredProjectSources


class ProjectDetector:
    """Detects likely database dialects and schema tool frameworks from discovered files."""

    @staticmethod
    def detect_dialect(
        sources: DiscoveredProjectSources,
        explicit_dialect: Dialect = Dialect.UNKNOWN,
    ) -> Dialect:
        """Determine the effective database dialect from explicit input or file evidence."""
        if explicit_dialect != Dialect.UNKNOWN:
            return explicit_dialect

        # If exclusively SQLite database files are found
        if sources.sqlite_files and not sources.sql_files:
            return Dialect.SQLITE

        # Sample SQL files to detect dialect
        detected_dialects: set[Dialect] = set()
        for sql_file in sources.sql_files[:10]:  # inspect up to first 10 files
            try:
                # Read sample (first 64KB)
                content = sql_file.read_text(encoding="utf-8", errors="ignore")[:65536]
                d = detect_dialect_from_sql(content)
                if d != Dialect.UNKNOWN:
                    detected_dialects.add(d)
            except Exception:
                continue

        if len(detected_dialects) == 1:
            return detected_dialects.pop()

        return Dialect.UNKNOWN

    @staticmethod
    def detect_framework_hints(sources: DiscoveredProjectSources) -> list[str]:
        """Detect schema tool conventions or framework directories."""
        hints: list[str] = []
        all_paths = [str(p).lower() for p in sources.sql_files + sources.sqlite_files]

        if any("alembic" in p for p in all_paths):
            hints.append("Alembic / SQLAlchemy migrations detected")
        if any("flyway" in p for p in all_paths):
            hints.append("Flyway database migrations detected")
        if any("prisma" in p for p in all_paths):
            hints.append("Prisma schema/migrations detected")
        if any("drizzle" in p for p in all_paths):
            hints.append("Drizzle ORM migrations detected")
        if any("db/migrate" in p or "db\\migrate" in p for p in all_paths):
            hints.append("ActiveRecord migrations detected")

        return hints
