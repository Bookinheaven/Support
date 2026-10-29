"""Connection health checking, connectivity verification, and status classification."""

import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional
import concurrent.futures
from sqlalchemy import text

from support.core.dialect import Dialect
from support.db.models import ConnectionProfile
from support.db.connection import DatabaseConnectionFactory


@dataclass
class HealthCheckResult:
    """Result of a database connectivity health check."""

    status: str  # ONLINE, OFFLINE, AUTH FAILED, TIMEOUT, ERROR, UNCHECKED
    latency_ms: Optional[float] = None
    server_version: Optional[str] = None
    error_message: Optional[str] = None
    hint: Optional[str] = None

    @property
    def is_online(self) -> bool:
        return self.status == "ONLINE"


def verify_driver_available(dialect: Dialect) -> tuple[bool, Optional[str]]:
    """Verify that the required DBAPI driver package is installed."""
    driver_packages = {
        Dialect.POSTGRES: "psycopg",
        Dialect.MYSQL: "pymysql",
        Dialect.MSSQL: "pymssql",
    }
    pkg = driver_packages.get(dialect)
    if not pkg:
        return True, None

    try:
        __import__(pkg)
        return True, None
    except ImportError:
        return False, f"Driver '{pkg}' is missing. Run: pip install {pkg}"


def classify_error(err_str: str, timeout_sec: float) -> tuple[str, str]:
    """Map driver error text into a canonical status code and a concise user hint."""
    err_low = err_str.lower()

    if any(k in err_low for k in ["timed out", "timeout", "time limit exceeded"]):
        return "TIMEOUT", f"Server did not respond within {timeout_sec:.0f}s. Check network connectivity and firewall."

    if any(
        k in err_low
        for k in [
            "password authentication failed",
            "access denied",
            "login failed",
            "authentication failed",
            "invalid password",
            "not a valid password",
        ]
    ):
        return "AUTH FAILED", "Authentication rejected. Check username and password."

    if any(
        k in err_low
        for k in [
            "connection refused",
            "actively refused",
            "could not connect",
            "not accessible",
            "unreachable",
            "failed to establish a new connection",
            "host is down",
            "network is unreachable",
            "name or service not known",
            "nodename nor servname provided",
            "file not found",
            "does not exist",
        ]
    ):
        return "OFFLINE", "Endpoint unreachable. Verify the database service is running and host/port are correct."

    return "ERROR", "Connection failed. Check connection parameters."


def check_connection_health(
    profile: ConnectionProfile,
    password: Optional[str] = None,
    timeout: float = 3.0,
) -> HealthCheckResult:
    """Execute a lightweight connectivity health check against a database profile."""
    # 1. Verify driver availability
    has_driver, driver_err = verify_driver_available(profile.dialect)
    if not has_driver:
        return HealthCheckResult(
            status="ERROR",
            error_message=driver_err,
            hint=driver_err,
        )

    # 2. Guard SQLite: never create missing files
    if profile.dialect == Dialect.SQLITE and profile.database != ":memory:":
        db_path = Path(profile.database)
        if not db_path.exists():
            return HealthCheckResult(
                status="OFFLINE",
                error_message=f"Database file '{profile.database}' not found.",
                hint="Verify the SQLite file path exists.",
            )

    start = time.perf_counter()
    try:
        engine = DatabaseConnectionFactory.create_engine(
            profile, password=password, timeout=int(timeout)
        )
        with engine.connect() as conn:
            query = "SELECT 1"
            if profile.dialect == Dialect.POSTGRES:
                query = "SELECT version()"
            elif profile.dialect == Dialect.MYSQL:
                query = "SELECT version()"
            elif profile.dialect == Dialect.MSSQL:
                query = "SELECT @@VERSION"
            elif profile.dialect == Dialect.SQLITE:
                query = "SELECT sqlite_version()"

            val = conn.execute(text(query)).scalar()
            latency = (time.perf_counter() - start) * 1000

            version_str = "Connected"
            if val:
                raw_ver = str(val).split("\n")[0].strip()
                # Clean up verbose version strings
                if "PostgreSQL" in raw_ver:
                    parts = raw_ver.split()
                    version_str = f"PostgreSQL {parts[1]}" if len(parts) > 1 else raw_ver
                elif "Microsoft SQL Server" in raw_ver:
                    version_str = "SQL Server"
                else:
                    version_str = f"{profile.dialect.display_name} {raw_ver}"

        engine.dispose()
        return HealthCheckResult(
            status="ONLINE",
            latency_ms=latency,
            server_version=version_str,
        )

    except Exception as e:
        latency = (time.perf_counter() - start) * 1000
        raw_err = str(e)
        if password and password in raw_err:
            raw_err = raw_err.replace(password, "***")

        status, hint = classify_error(raw_err, timeout)
        return HealthCheckResult(
            status=status,
            latency_ms=latency,
            error_message=raw_err,
            hint=hint,
        )


def check_profiles_concurrently(
    profiles: list[ConnectionProfile],
    passwords: dict[str, Optional[str]],
    max_workers: int = 8,
    timeout: float = 3.0,
) -> dict[str, HealthCheckResult]:
    """Check connectivity for multiple profiles in parallel."""
    results: dict[str, HealthCheckResult] = {}
    if not profiles:
        return results

    workers = min(len(profiles), max_workers)
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as executor:
        future_map = {
            executor.submit(
                check_connection_health, p, passwords.get(p.name), timeout
            ): p.name
            for p in profiles
        }

        for future in concurrent.futures.as_completed(future_map):
            p_name = future_map[future]
            try:
                results[p_name] = future.result()
            except Exception as e:
                results[p_name] = HealthCheckResult(
                    status="ERROR",
                    error_message=str(e),
                    hint="Unexpected probe failure.",
                )

    return results
