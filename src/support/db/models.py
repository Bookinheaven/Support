"""Connection profile data models."""

from typing import Any, Optional
from pydantic import BaseModel, Field, ConfigDict
from support.core.dialect import Dialect


class ConnectionProfile(BaseModel):
    """Database connection profile metadata (non-secret)."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    name: str
    dialect: Dialect
    host: Optional[str] = None
    port: Optional[int] = None
    database: str
    username: Optional[str] = None
    options: dict[str, Any] = Field(default_factory=dict)

    @property
    def display_target(self) -> str:
        """Formatted representation of the database target without credentials."""
        if self.dialect == Dialect.SQLITE:
            return self.database
        host_str = self.host or "localhost"
        port_str = f":{self.port}" if self.port else ""
        return f"{host_str}{port_str}/{self.database}"

    def get_redacted_url(self) -> str:
        """Generate a connection URL with the password redacted."""
        if self.dialect == Dialect.SQLITE:
            return f"sqlite:///{self.database}"

        if self.dialect == Dialect.MONGODB:
            driver = "mongodb+srv" if self.options.get("srv") else "mongodb"
            user_part = f"{self.username}:***@" if self.username else ""
            host_str = self.host or "127.0.0.1"
            port_str = f":{self.port}" if (self.port and not self.options.get("srv")) else ""
            return f"{driver}://{user_part}{host_str}{port_str}/{self.database}"

        driver_map = {
            Dialect.POSTGRES: "postgresql+psycopg",
            Dialect.MYSQL: "mysql+pymysql",
            Dialect.MSSQL: "mssql+pymssql",
        }
        driver = driver_map.get(self.dialect, self.dialect.value)
        user_part = f"{self.username}:***@" if self.username else ""
        host_str = self.host or "127.0.0.1"
        port_str = f":{self.port}" if self.port else ""
        return f"{driver}://{user_part}{host_str}{port_str}/{self.database}"
