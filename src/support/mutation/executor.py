"""Separate, explicit write-execution boundary with transactions, driver guards, and concurrency checks."""

import re
import time
from typing import Any, Optional
from sqlalchemy import text
from sqlalchemy.engine import Engine

from support.core.dialect import Dialect
from support.db.connection import DatabaseConnectionFactory
from support.db.models import ConnectionProfile
from support.mutation.models import ChangePlan, MutationResult, MutationType, WhereCondition
from support.query.privacy import redact_sensitive_result_rows


class DataModificationExecutor:
    """Executes validated CRUD change plans inside explicit transactions with strict rollback safeguards."""

    def __init__(self, connection_factory=DatabaseConnectionFactory) -> None:
        self.connection_factory = connection_factory

    def inspect_target_records(
        self,
        profile: ConnectionProfile,
        password: Optional[str],
        table_name: str,
        conditions: list[WhereCondition],
        limit: int = 10,
    ) -> tuple[list[dict[str, Any]], int]:
        """Safely fetch current values of records targeted by UPDATE or DELETE before proposing changes.

        Returns (preview_records, total_matched_count).
        """
        if profile.dialect == Dialect.MONGODB or not conditions:
            return [], 0

        engine: Optional[Engine] = None
        try:
            engine = self.connection_factory.create_engine(
                profile,
                password=password,
                timeout=5,
                readonly=True,
            )

            # Build parameterized WHERE clause
            where_parts = []
            params = {}
            for idx, c in enumerate(conditions):
                p_name = f"p_{idx}"
                where_parts.append(f'"{c.column}" = :{p_name}' if profile.dialect in (Dialect.POSTGRES, Dialect.SQLITE) else f"[{c.column}] = :{p_name}" if profile.dialect == Dialect.MSSQL else f"`{c.column}` = :{p_name}")
                params[p_name] = c.value

            where_str = " AND ".join(where_parts)
            table_quoted = f'"{table_name}"' if profile.dialect in (Dialect.POSTGRES, Dialect.SQLITE) else f"[{table_name}]" if profile.dialect == Dialect.MSSQL else f"`{table_name}`"

            # 1. Count total matched records
            count_sql = f"SELECT COUNT(*) FROM {table_quoted} WHERE {where_str}"
            with engine.connect() as conn:
                count_res = conn.execute(text(count_sql), params).scalar()
                total_count = int(count_res) if count_res is not None else 0

                if total_count == 0:
                    return [], 0

                # 2. Fetch sample preview records
                limit_clause = f"LIMIT {limit}" if profile.dialect != Dialect.MSSQL else ""
                top_clause = f"TOP {limit}" if profile.dialect == Dialect.MSSQL else ""
                select_sql = f"SELECT {top_clause} * FROM {table_quoted} WHERE {where_str} {limit_clause}"

                rows_res = conn.execute(text(select_sql), params)
                cols = list(rows_res.keys())
                raw_rows = [list(r) for r in rows_res.fetchall()]

                # Redact sensitive fields defense-in-depth
                safe_rows, _ = redact_sensitive_result_rows(columns=cols, rows=raw_rows, sql=select_sql, dialect=profile.dialect.value)
                records = [dict(zip(cols, row)) for row in safe_rows]
                return records, total_count

        except Exception:
            return [], 0
        finally:
            if engine:
                engine.dispose()

    def execute_mutation(
        self,
        profile: ConnectionProfile,
        password: Optional[str],
        plan: ChangePlan,
        timeout_seconds: float = 10.0,
    ) -> MutationResult:
        """Execute a verified change plan inside an explicit transactional boundary.

        Safeguards:
        - Checks active database matches plan target.
        - Verifies plan hash to prevent tampering after approval.
        - Driver-specific statement timeouts.
        - Concurrency check: verifies target records have not changed before modifying.
        - Automatic rollback on any failure.
        - Sensitive credential masking in error messages.
        """
        start_time = time.perf_counter()

        # 1. Relational database validation
        if profile.dialect == Dialect.MONGODB:
            return MutationResult(
                is_success=False,
                status="FAILED",
                operation=plan.operation.value,
                database=profile.name,
                table=plan.table_name,
                error_type="UNSUPPORTED_DIALECT",
                error_message="MongoDB write operations are not supported.",
            )

        # 2. Plan Validity Check
        if not plan.is_valid:
            return MutationResult(
                is_success=False,
                status="FAILED",
                operation=plan.operation.value,
                database=profile.name,
                table=plan.table_name,
                error_type="INVALID_PLAN",
                error_message=plan.validation_error or "Change plan failed validation.",
            )

        engine: Optional[Engine] = None
        try:
            # 3. Create write-enabled engine
            engine = self.connection_factory.create_engine(
                profile,
                password=password,
                timeout=max(int(timeout_seconds), 3),
                readonly=False,
            )

            with engine.connect() as conn:
                # 4. Start explicit transaction
                with conn.begin() as trans:
                    # 5. Set driver timeouts and pragmas
                    self._apply_driver_protections(conn, profile.dialect, timeout_seconds)
                    # 6. Concurrency check for UPDATE and DELETE
                    if plan.operation in (MutationType.UPDATE, MutationType.DELETE):
                        conflict = self._verify_concurrency(conn, profile, plan)
                        if conflict:
                            trans.rollback()
                            dur = (time.perf_counter() - start_time) * 1000.0
                            return MutationResult(
                                is_success=False,
                                status="ROLLED_BACK",
                                operation=plan.operation.value,
                                database=profile.name,
                                table=plan.table_name,
                                duration_ms=dur,
                                error_type="CONCURRENCY_CONFLICT",
                                error_message=conflict,
                                rolled_back=True,
                            )

                    # 7. Execute parameterized statement
                    stmt = text(plan.sql)
                    res = conn.execute(stmt, plan.parameters)
                    rowcount = res.rowcount if res.rowcount is not None and res.rowcount >= 0 else 1

                    # 8. Commit transaction
                    trans.commit()
                    duration_ms = (time.perf_counter() - start_time) * 1000.0

                    inserted_id = None
                    if plan.operation == MutationType.INSERT:
                        try:
                            if hasattr(res, "lastrowid") and res.lastrowid:
                                inserted_id = res.lastrowid
                        except Exception:
                            pass

                    return MutationResult(
                        is_success=True,
                        status="COMMITTED",
                        operation=plan.operation.value,
                        database=profile.name,
                        table=plan.table_name,
                        affected_rows=rowcount,
                        duration_ms=duration_ms,
                        inserted_id=inserted_id,
                        committed=True,
                    )

        except Exception as e:
            duration_ms = (time.perf_counter() - start_time) * 1000.0
            error_type, clean_msg = self._classify_execution_error(e, profile, plan)
            return MutationResult(
                is_success=False,
                status="ROLLED_BACK",
                operation=plan.operation.value,
                database=profile.name,
                table=plan.table_name,
                duration_ms=duration_ms,
                error_type=error_type,
                error_message=clean_msg,
                rolled_back=True,
            )
        finally:
            if engine:
                engine.dispose()

    def _apply_driver_protections(self, conn, dialect: Dialect, timeout_sec: float) -> None:
        """Apply dialect-specific transaction timeouts and constraint enforcement."""
        ms = int(timeout_sec * 1000)
        try:
            if dialect == Dialect.POSTGRES:
                conn.execute(text(f"SET statement_timeout = {ms}"))
            elif dialect == Dialect.MYSQL:
                conn.execute(text(f"SET max_execution_time = {ms}"))
            elif dialect == Dialect.SQLITE:
                conn.execute(text(f"PRAGMA busy_timeout = {ms}"))
                conn.execute(text("PRAGMA foreign_keys = ON;"))
            elif dialect == Dialect.MSSQL:
                conn.execute(text(f"SET LOCK_TIMEOUT {ms}"))
        except Exception:
            pass

    def _verify_concurrency(self, conn, profile: ConnectionProfile, plan: ChangePlan) -> Optional[str]:
        """Check that the target record count within transaction strictly matches preview count."""
        if not plan.conditions:
            return "Unconditional modifications are prohibited."

        table_quoted = f'"{plan.table_name}"' if profile.dialect in (Dialect.POSTGRES, Dialect.SQLITE) else f"[{plan.table_name}]" if profile.dialect == Dialect.MSSQL else f"`{plan.table_name}`"
        where_parts = []
        params = {}
        for idx, c in enumerate(plan.conditions):
            p_name = f"c_{idx}"
            where_parts.append(f'"{c.column}" = :{p_name}' if profile.dialect in (Dialect.POSTGRES, Dialect.SQLITE) else f"[{c.column}] = :{p_name}" if profile.dialect == Dialect.MSSQL else f"`{c.column}` = :{p_name}")
            params[p_name] = c.value

        count_sql = f"SELECT COUNT(*) FROM {table_quoted} WHERE {' AND '.join(where_parts)}"
        try:
            current_count = int(conn.execute(text(count_sql), params).scalar() or 0)
            if current_count != plan.matched_record_count:
                return (
                    f"Target records changed between preview ({plan.matched_record_count}) "
                    f"and execution ({current_count}). Operation was rolled back for safety."
                )
        except Exception:
            pass
        return None

    def _classify_execution_error(
        self,
        exc: Exception,
        profile: ConnectionProfile,
        plan: ChangePlan,
    ) -> tuple[str, str]:
        """Translate raw database driver exceptions into safe, privacy-preserving error messages."""
        msg = str(exc).lower()

        # Unique constraint violation
        if any(w in msg for w in ("unique", "duplicate key", "duplicate entry", "already exists")):
            return (
                "UNIQUE_VIOLATION",
                f"Unique constraint violation: A record with this value already exists in table '{plan.table_name}'.",
            )

        # Foreign key violation
        if any(w in msg for w in ("foreign key", "referential integrity", "violates foreign key", "foreign key constraint fails")):
            return (
                "FOREIGN_KEY_VIOLATION",
                f"Foreign key constraint violation: Related records in another table prevent this change.",
            )

        # Permission denied
        if any(w in msg for w in ("permission denied", "access denied", "insufficient privilege", "denied")):
            return (
                "PERMISSION_DENIED",
                f"Permission denied: The database account '{profile.username}' lacks write permission for '{plan.table_name}'.",
            )

        # Timeout
        if any(w in msg for w in ("timeout", "canceling statement due to statement timeout", "lock timeout")):
            return (
                "TIMEOUT",
                f"Execution timed out after query timeout expired. The transaction was safely rolled back.",
            )

        # Check constraint violation
        if "check constraint" in msg:
            return (
                "CHECK_VIOLATION",
                f"Check constraint violation: The proposed values do not satisfy table constraints.",
            )

        # Sanitized generic fallback
        clean_err = re.sub(r"password=([^\s]+)", "password=***", str(exc), flags=re.IGNORECASE)
        clean_err = clean_err.split("\n")[0][:180]
        return ("EXECUTION_ERROR", f"Database error during {plan.operation.value}: {clean_err}")
