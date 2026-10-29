"""Database connection factory and connectivity verification."""

import time
from pathlib import Path
from typing import Optional, Any
from sqlalchemy import create_engine, Engine, text
from sqlalchemy.engine import URL

from support.core.dialect import Dialect
from support.core.exceptions import DatabaseAccessError, UnsupportedDialectError
from support.db.models import ConnectionProfile


class DatabaseConnectionFactory:
    """Creates SQLAlchemy database engines and executes connectivity checks."""

    @staticmethod
    def get_sqlalchemy_url(
        profile: ConnectionProfile,
        password: Optional[str] = None,
        hide_password: bool = False,
    ) -> str:
        """Construct the SQLAlchemy connection URL with or without masked credentials."""
        if profile.dialect == Dialect.SQLITE:
            return f"sqlite:///{profile.database}"

        driver_map = {
            Dialect.POSTGRES: "postgresql+psycopg",
            Dialect.MYSQL: "mysql+pymysql",
            Dialect.MSSQL: "mssql+pymssql",
        }

        driver = driver_map.get(profile.dialect)
        if not driver:
            raise UnsupportedDialectError(profile.dialect.value)

        # Merge profile options into query dictionary
        query_params: dict[str, Any] = dict(profile.options)
        if profile.dialect == Dialect.MYSQL and "charset" not in query_params:
            query_params["charset"] = "utf8mb4"

        url = URL.create(
            drivername=driver,
            username=profile.username,
            password=password if not hide_password else "***",
            host=profile.host or "127.0.0.1",
            port=profile.port,
            database=profile.database,
            query=query_params if query_params else None,
        )

        return url.render_as_string(hide_password=hide_password)

    @classmethod
    def create_engine(
        cls,
        profile: ConnectionProfile,
        password: Optional[str] = None,
        timeout: int = 10,
    ) -> Engine:
        """Create a SQLAlchemy Engine for the given profile and credentials."""
        if profile.dialect == Dialect.SQLITE:
            if profile.database == ":memory:":
                return create_engine("sqlite:///:memory:")
            db_path = Path(profile.database)
            if not db_path.exists():
                raise DatabaseAccessError(
                    str(profile.database),
                    f"SQLite database file '{profile.database}' does not exist.",
                )
            return create_engine(f"sqlite:///file:{db_path.resolve().as_posix()}?mode=ro&uri=true")

        driver_map = {
            Dialect.POSTGRES: "postgresql+psycopg",
            Dialect.MYSQL: "mysql+pymysql",
            Dialect.MSSQL: "mssql+pymssql",
        }
        driver = driver_map.get(profile.dialect)
        if not driver:
            raise UnsupportedDialectError(profile.dialect.value)

        query_params: dict[str, Any] = dict(profile.options)
        if profile.dialect == Dialect.MYSQL and "charset" not in query_params:
            query_params["charset"] = "utf8mb4"

        url_obj = URL.create(
            drivername=driver,
            username=profile.username,
            password=password,
            host=profile.host or "127.0.0.1",
            port=profile.port,
            database=profile.database,
            query=query_params if query_params else None,
        )

        connect_args: dict[str, Any] = {}
        if profile.dialect == Dialect.POSTGRES:
            connect_args["connect_timeout"] = timeout
        elif profile.dialect == Dialect.MYSQL:
            connect_args["connect_timeout"] = timeout
        elif profile.dialect == Dialect.MSSQL:
            connect_args["timeout"] = timeout

        try:
            return create_engine(
                url_obj,
                connect_args=connect_args,
                pool_pre_ping=True,
                pool_recycle=3600,
            )
        except Exception as e:
            redacted = cls.get_sqlalchemy_url(profile, hide_password=True)
            raise DatabaseAccessError(
                redacted,
                f"Failed to initialize database engine: {e}",
            )

    @classmethod
    def test_connection(
        cls,
        profile: ConnectionProfile,
        password: Optional[str] = None,
        timeout: int = 10,
    ) -> tuple[bool, str, Optional[str], float]:
        """Test database connection.

        Returns (success: bool, message: str, server_version: Optional[str], latency_ms: float).
        """
        start = time.perf_counter()

        # Guard SQLite: do not create missing files
        if profile.dialect == Dialect.SQLITE and profile.database != ":memory:":
            db_path = Path(profile.database)
            if not db_path.exists():
                return False, f"SQLite database file '{profile.database}' does not exist.", None, 0.0

        try:
            engine = cls.create_engine(profile, password=password, timeout=timeout)
            with engine.connect() as conn:
                version_sql = "SELECT 1"
                if profile.dialect == Dialect.POSTGRES:
                    version_sql = "SELECT version()"
                elif profile.dialect == Dialect.MYSQL:
                    version_sql = "SELECT version()"
                elif profile.dialect == Dialect.MSSQL:
                    version_sql = "SELECT @@VERSION"
                elif profile.dialect == Dialect.SQLITE:
                    version_sql = "SELECT sqlite_version()"

                result = conn.execute(text(version_sql)).scalar()
                latency_ms = (time.perf_counter() - start) * 1000
                server_info = str(result).split("\n")[0].strip() if result else "Connected"

            engine.dispose()
            return True, "Connection successful", server_info, latency_ms

        except Exception as e:
            latency_ms = (time.perf_counter() - start) * 1000
            err_msg = str(e)

            # Redact password if it leaked into the error message
            if password and password in err_msg:
                err_msg = err_msg.replace(password, "***")

            # Check for common missing driver issues
            if "No module named 'psycopg'" in err_msg:
                err_msg += " (Install driver: pip install psycopg)"
            elif "No module named 'pymysql'" in err_msg:
                err_msg += " (Install driver: pip install pymysql)"
            elif "No module named 'pymssql'" in err_msg:
                err_msg += " (Install driver: pip install pymssql)"

            return False, err_msg, None, latency_ms

    @staticmethod
    def classify_status(success: bool, error_message: Optional[str]) -> str:
        """Classify probe outcome into ONLINE, OFFLINE, AUTH FAILED, TIMEOUT, or ERROR."""
        if success:
            return "ONLINE"
        if not error_message:
            return "OFFLINE"

        err_low = error_message.lower()
        if any(k in err_low for k in ["timed out", "timeout", "time limit exceeded"]):
            return "TIMEOUT"
        if any(
            k in err_low
            for k in [
                "password authentication failed",
                "access denied",
                "login failed",
                "authentication failed",
            ]
        ):
            return "AUTH FAILED"
        if any(
            k in err_low
            for k in [
                "connection refused",
                "actively refused",
                "could not connect",
                "not accessible",
                "unreachable",
                "does not exist",
                "no such file",
                "failed to establish a new connection",
                "host is down",
                "network is unreachable",
                "name or service not known",
                "nodename nor servname provided",
            ]
        ):
            return "OFFLINE"

        return "ERROR"
