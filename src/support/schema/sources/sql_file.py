"""Schema source adapter for raw SQL DDL files."""

from pathlib import Path
from typing import Optional
from support.core.dialect import Dialect
from support.core.exceptions import SQLParseError
from support.schema.base import BaseSchemaSource, SchemaDiscoveryResult, DiscoveryWarning
from support.schema.models import DatabaseSchema
from support.schema.sql_parser import SQLDDLParser
from support.schema.merger import SchemaMerger


class SQLFileSchemaSource(BaseSchemaSource):
    """Discovers database schema from one or more raw SQL files."""

    def __init__(
        self,
        files: list[Path] | Path,
        dialect: Dialect = Dialect.UNKNOWN,
        max_file_size_bytes: int = 10 * 1024 * 1024,
    ) -> None:
        self.files = [files] if isinstance(files, Path) else list(files)
        self.dialect = dialect
        self.max_file_size_bytes = max_file_size_bytes
        self.parser = SQLDDLParser(dialect=dialect)

    def discover(self) -> SchemaDiscoveryResult:
        """Parse all configured SQL files and return the combined schema discovery result."""
        discovered_files = list(self.files)
        parsed_files: list[Path] = []
        failed_files: dict[str, str] = {}
        all_warnings: list[DiscoveryWarning] = []
        parsed_schemas: list[DatabaseSchema] = []

        for file_path in self.files:
            if not file_path.exists():
                failed_files[str(file_path)] = "File not found"
                continue

            # Check file size
            try:
                size = file_path.stat().st_size
                if size > self.max_file_size_bytes:
                    all_warnings.append(
                        DiscoveryWarning(
                            message=f"File exceeds maximum size ({size} bytes > {self.max_file_size_bytes} bytes). Skipping.",
                            file_path=str(file_path),
                            severity="warning",
                        )
                    )
                    continue
            except Exception as e:
                failed_files[str(file_path)] = f"Cannot read file stat: {e}"
                continue

            # Read content
            content: Optional[str] = None
            for encoding in ("utf-8", "utf-8-sig", "latin-1"):
                try:
                    content = file_path.read_text(encoding=encoding)
                    break
                except UnicodeDecodeError:
                    continue
                except Exception as e:
                    failed_files[str(file_path)] = f"Read error: {e}"
                    break

            if content is None:
                if str(file_path) not in failed_files:
                    failed_files[str(file_path)] = "Failed to decode file with supported encodings"
                continue

            # Parse SQL
            try:
                file_schema, file_warnings = self.parser.parse_sql(
                    content, source_origin=str(file_path)
                )
                parsed_schemas.append(file_schema)
                parsed_files.append(file_path)
                all_warnings.extend(file_warnings)
            except SQLParseError as e:
                failed_files[str(file_path)] = str(e.details or e.message)
            except Exception as e:
                failed_files[str(file_path)] = f"Parsing error: {e}"

        merged_schema, merge_warnings = SchemaMerger.merge(
            parsed_schemas, forced_dialect=self.dialect
        )
        all_warnings.extend(merge_warnings)

        return SchemaDiscoveryResult(
            schema=merged_schema,
            discovered_files=discovered_files,
            parsed_files=parsed_files,
            failed_files=failed_files,
            warnings=all_warnings,
        )
