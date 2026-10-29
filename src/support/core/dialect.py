"""Database dialect definitions and parsing utilities."""

from enum import Enum
from typing import Optional


class Dialect(str, Enum):
    """Supported database dialects."""

    POSTGRES = "postgres"
    MYSQL = "mysql"
    SQLITE = "sqlite"
    MSSQL = "mssql"
    MONGODB = "mongodb"
    UNKNOWN = "unknown"

    @classmethod
    def from_string(cls, value: Optional[str]) -> "Dialect":
        """Convert a user string or identifier to a canonical Dialect enum."""
        if not value:
            return cls.UNKNOWN

        clean = value.strip().lower().replace("-", "_").replace(" ", "_")

        if clean in ("postgres", "postgresql", "pg", "pgsql"):
            return cls.POSTGRES
        if clean in ("mysql", "mariadb"):
            return cls.MYSQL
        if clean in ("sqlite", "sqlite3"):
            return cls.SQLITE
        if clean in ("mssql", "sqlserver", "tsql", "microsoft_sql_server", "ms_sql"):
            return cls.MSSQL
        if clean in ("mongodb", "mongo"):
            return cls.MONGODB
        if clean in ("unknown", "generic"):
            return cls.UNKNOWN

        return cls.UNKNOWN

    @property
    def is_document_database(self) -> bool:
        """True if dialect is a document database such as MongoDB."""
        return self == self.MONGODB

    @property
    def is_relational(self) -> bool:
        """True if dialect is a relational/SQL database."""
        return self in (self.POSTGRES, self.MYSQL, self.SQLITE, self.MSSQL)

    @property
    def sqlglot_read_dialect(self) -> Optional[str]:
        """Map dialect to sqlglot read dialect name."""
        mapping = {
            self.POSTGRES: "postgres",
            self.MYSQL: "mysql",
            self.SQLITE: "sqlite",
            self.MSSQL: "tsql",
            self.MONGODB: None,
            self.UNKNOWN: None,
        }
        return mapping.get(self)

    @property
    def display_name(self) -> str:
        """User-friendly display name."""
        names = {
            self.POSTGRES: "PostgreSQL",
            self.MYSQL: "MySQL",
            self.SQLITE: "SQLite",
            self.MSSQL: "Microsoft SQL Server",
            self.MONGODB: "MongoDB",
            self.UNKNOWN: "Unknown / Generic SQL",
        }
        return names.get(self, "Unknown")
