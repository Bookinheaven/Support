"""Controlled, dialect-aware, read-only SQL query executor with driver-level guards."""

from dataclasses import dataclass, field
from pathlib import Path
import time
from typing import Any, Optional

from sqlalchemy import Engine, text

from support.core.dialect import Dialect
from support.db.connection import DatabaseConnectionFactory
from support.db.models import ConnectionProfile


@dataclass
class ExecutionConfig:
    """Configuration parameters for read-only query execution."""

    max_rows: int = 100
    timeout_seconds: float = 10.0
    batch_size: int = 50


@dataclass
class QueryResult:
    """Result of an executed read-only SQL query."""

    columns: list[str] = field(default_factory=list)
    rows: list[list[Any]] = field(default_factory=list)
    row_count: int = 0
    is_truncated: bool = False
    duration_ms: float = 0.0
    executed_sql: str = ""
    database: str = ""
    dialect: str = ""
    safeguard_applied: str = ""
    error_message: Optional[str] = None
    is_timeout: bool = False
    is_permission_error: bool = False
    is_readonly_error: bool = False

    @property
    def is_success(self) -> bool:
        """True if the query completed without errors."""
        return self.error_message is None


class ReadOnlyQueryExecutor:
    """Executes validated SQL queries with dialect-specific read-only safeguards and bounded fetching."""

    def __init__(self, connection_factory=DatabaseConnectionFactory) -> None:
        self.connection_factory = connection_factory

    def execute(
        self,
        profile: ConnectionProfile,
        password: Optional[str],
        sql: str,
        config: Optional[ExecutionConfig] = None,
    ) -> QueryResult:
        """Execute a read-only SQL query with database-specific safeguards and timeouts.

        Args:
            profile: Target database connection profile.
            password: Optional database credential.
            sql: Pre-validated SQL string to execute.
            config: Execution limits (max rows, timeout, batch size).

        Returns:
            QueryResult containing rows, columns, timing, and truncation status.
        """
        cfg = config or ExecutionConfig()
        dialect = profile.dialect
        start_time = time.perf_counter()

        if dialect == Dialect.MONGODB:
            return QueryResult(
                executed_sql=sql,
                database=profile.database or profile.name,
                dialect=dialect.value,
                error_message="MongoDB query execution is not supported. Only relational SQL execution is supported.",
            )

        # Guard SQLite missing file check before engine creation
        if dialect == Dialect.SQLITE and profile.database != ":memory:":
            db_path = Path(profile.database)
            if not db_path.exists():
                return QueryResult(
                    executed_sql=sql,
                    database=profile.database,
                    dialect=dialect.value,
                    error_message=f"SQLite database file '{profile.database}' does not exist.",
                )

        from support.core.performance import get_current_tracker
        tracker = get_current_tracker()
        engine: Optional[Engine] = None
        try:
            t_conn0 = time.perf_counter()
            engine = self.connection_factory.create_engine(
                profile,
                password=password,
                timeout=max(int(cfg.timeout_seconds), 3),
            )
            with engine.connect() as conn:
                if tracker:
                    tracker.stage_durations["db_connection"] = (
                        tracker.stage_durations.get("db_connection", 0.0) + (time.perf_counter() - t_conn0) * 1000.0
                    )
                safeguard_info = self._apply_safeguards_and_timeout(conn, dialect, cfg, profile)

                # Execute with driver
                t_exec0 = time.perf_counter()
                result_proxy = conn.execute(text(sql))
                columns = list(result_proxy.keys()) if result_proxy.returns_rows else []
                rows: list[list[Any]] = []
                is_truncated = False

                if result_proxy.returns_rows:
                    while len(rows) < cfg.max_rows:
                        fetch_limit = min(cfg.batch_size, cfg.max_rows - len(rows))
                        batch = result_proxy.fetchmany(fetch_limit)
                        if not batch:
                            break
                        for row in batch:
                            rows.append(list(row))

                    # Bounded check for truncation: probe if at least 1 more row exists
                    if len(rows) >= cfg.max_rows:
                        extra = result_proxy.fetchmany(1)
                        if extra:
                            is_truncated = True

                if tracker:
                    tracker.stage_durations["db_execution"] = (
                        tracker.stage_durations.get("db_execution", 0.0) + (time.perf_counter() - t_exec0) * 1000.0
                    )

                duration_ms = (time.perf_counter() - start_time) * 1000

                return QueryResult(
                    columns=columns,
                    rows=rows,
                    row_count=len(rows),
                    is_truncated=is_truncated,
                    duration_ms=duration_ms,
                    executed_sql=sql,
                    database=profile.database or profile.name,
                    dialect=dialect.value,
                    safeguard_applied=safeguard_info,
                )

        except Exception as exc:
            duration_ms = (time.perf_counter() - start_time) * 1000
            err_str = str(exc)
            if password and password in err_str:
                err_str = err_str.replace(password, "***")

            is_timeout = self._is_timeout_error(err_str)
            is_readonly = self._is_readonly_error(err_str)
            is_perm = self._is_permission_error(err_str)

            clean_error = err_str
            if is_timeout:
                clean_error = f"Execution timed out after {cfg.timeout_seconds:.1f}s."
            elif is_readonly:
                clean_error = "Execution rejected: Attempted data mutation in a read-only session."
            elif is_perm:
                clean_error = "Execution rejected: Insufficient database privileges."

            return QueryResult(
                executed_sql=sql,
                database=profile.database or profile.name,
                dialect=dialect.value,
                duration_ms=duration_ms,
                error_message=clean_error,
                is_timeout=is_timeout,
                is_readonly_error=is_readonly,
                is_permission_error=is_perm,
            )
        finally:
            if engine is not None:
                try:
                    engine.dispose()
                except Exception:
                    pass

    def _apply_safeguards_and_timeout(
        self,
        conn: Any,
        dialect: Dialect,
        cfg: ExecutionConfig,
        profile: ConnectionProfile,
    ) -> str:
        """Apply dialect-specific read-only guards and timeout configurations to active connection."""
        timeout_ms = int(cfg.timeout_seconds * 1000)

        if dialect == Dialect.POSTGRES:
            conn.execute(text("SET TRANSACTION READ ONLY;"))
            conn.execute(text(f"SET LOCAL statement_timeout = {timeout_ms};"))
            return f"PostgreSQL read-only transaction (statement_timeout={timeout_ms}ms)"

        elif dialect == Dialect.MYSQL:
            conn.execute(text("SET SESSION TRANSACTION READ ONLY;"))
            try:
                conn.execute(text(f"SET SESSION max_execution_time = {timeout_ms};"))
            except Exception:
                pass  # MariaDB or MySQL < 5.7.8
            return f"MySQL session read-only (max_execution_time={timeout_ms}ms)"

        elif dialect == Dialect.SQLITE:
            conn.execute(text("PRAGMA query_only = ON;"))
            # Apply progress handler timeout to SQLite raw dbapi connection
            try:
                raw_conn = getattr(conn.connection, "dbapi_connection", conn.connection)
                if hasattr(raw_conn, "set_progress_handler"):
                    deadline = time.monotonic() + cfg.timeout_seconds

                    def _progress() -> int:
                        if time.monotonic() > deadline:
                            return 1  # Non-zero aborts query with OperationalError: interrupted
                        return 0

                    raw_conn.set_progress_handler(_progress, 100)
            except Exception:
                pass
            return f"SQLite PRAGMA query_only=ON (progress_handler timeout={cfg.timeout_seconds:.1f}s)"

        elif dialect == Dialect.MSSQL:
            # Check if user has restricted read-only roles
            allow_unrestricted = profile.options.get("allow_unrestricted_exec", False)
            if not allow_unrestricted:
                try:
                    is_sysadmin = conn.execute(text("SELECT IS_SRVROLEMEMBER('sysadmin')")).scalar()
                    is_dbowner = conn.execute(text("SELECT IS_ROLEMEMBER('db_owner')")).scalar()
                    is_deny = conn.execute(text("SELECT IS_ROLEMEMBER('db_denydatawriter')")).scalar()
                    if (is_sysadmin == 1 or is_dbowner == 1) and is_deny != 1:
                        raise PermissionError(
                            "SQL Server does not provide a session-level read-only command. "
                            "Active account has unrestricted write privileges (sysadmin or db_owner). "
                            "To execute safely, please configure a dedicated read-only database account "
                            "assigned the 'db_datareader' and 'db_denydatawriter' roles."
                        )
                except PermissionError:
                    raise
                except Exception:
                    pass

            conn.execute(text(f"SET LOCK_TIMEOUT {timeout_ms};"))
            return f"SQL Server permission guard (query_timeout={int(cfg.timeout_seconds)}s, lock_timeout={timeout_ms}ms)"

        return "Default guard"

    @staticmethod
    def _is_timeout_error(err_msg: str) -> bool:
        low = err_msg.lower()
        return any(
            t in low
            for t in (
                "statement timeout",
                "canceling statement due to statement timeout",
                "max_execution_time exceeded",
                "query execution was interrupted (max_statement_time exceeded)",
                "operationalerror: interrupted",
                "operationalerror) interrupted",
                "interrupted",
                "tds server connection timed out",
                "timed out",
                "timeout exceeded",
                "time limit exceeded",
            )
        )

    @staticmethod
    def _is_readonly_error(err_msg: str) -> bool:
        low = err_msg.lower()
        return any(
            t in low
            for t in (
                "cannot execute create table in a read-only transaction",
                "cannot execute statement in a read only transaction",
                "cannot execute in a read-only transaction",
                "attempt to write a readonly database",
                "transaction is read-only",
                "read-only transaction",
            )
        )

    @staticmethod
    def _is_permission_error(err_msg: str) -> bool:
        low = err_msg.lower()
        return any(
            t in low
            for t in (
                "permission denied",
                "access denied",
                "permissionerror",
                "unrestricted write privileges",
            )
        )
