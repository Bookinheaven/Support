"""Multi-table inspection service for deterministic schema-driven row sampling.

Safely handles requests such as:
- 'Display all tables and print the first row from each table.'
- 'Give me a quick overview of the data in every table.'
- 'Show the first three records from users and tasks.'

Key Safeguards:
- Deterministic query construction (no multi-statement LLM scripts).
- Dialect-aware identifier quoting and row limiting.
- Exclusion of sensitive columns using centralized privacy policy.
- Skipping tables with no safe columns with clear explanation.
- Cryptographic plan hash verification preventing post-approval modification.
- Explicit approval with Enter defaulting to No.
- Independent table execution: failure on one table does not prevent inspecting others.
- Never retries failed queries automatically.
- Bounded row limit and query timeouts.
- History recording without persisting result rows.
"""

from dataclasses import dataclass, field
import hashlib
import os
import time
from typing import Callable, Optional

from sqlglot import exp

from support.cli.display import console
from support.core.dialect import Dialect
from support.core.formatting import format_duration, format_timeout
from support.db.models import ConnectionProfile
from support.db.service import DatabaseService
from support.history.store import QueryHistoryStore
from support.query.display import display_query_result
from support.query.executor import ExecutionConfig, QueryResult, ReadOnlyQueryExecutor
from support.query.privacy import is_sensitive_column_name
from support.query.schema_context import inspect_database_schema
from support.query.validator import validate_sql_query
from support.schema.models import DatabaseSchema, TableSchema
from support.workspace.state import WorkspaceSessionState

DEFAULT_MAX_INSPECTION_TABLES = 20


@dataclass
class TableInspectionItem:
    """Inspection plan for an individual table."""

    table_name: str
    schema_name: Optional[str] = None
    display_name: str = ""
    columns: list[str] = field(default_factory=list)
    sensitive_columns_excluded: list[str] = field(default_factory=list)
    sql: Optional[str] = None
    is_skipped: bool = False
    skip_reason: Optional[str] = None

    def __post_init__(self) -> None:
        if not self.display_name:
            if self.schema_name:
                self.display_name = f"{self.schema_name}.{self.table_name}"
            else:
                self.display_name = self.table_name


@dataclass
class MultiTableInspectionPlan:
    """Verified multi-table inspection plan awaiting approval."""

    database: str
    dialect: Dialect
    row_limit: int
    tables: list[TableInspectionItem] = field(default_factory=list)
    total_database_tables: int = 0
    is_capped: bool = False
    cap_limit: int = DEFAULT_MAX_INSPECTION_TABLES
    plan_hash: str = ""

    @property
    def eligible_tables(self) -> list[TableInspectionItem]:
        """Tables ready for execution."""
        return [t for t in self.tables if not t.is_skipped and t.sql]

    @property
    def skipped_tables(self) -> list[TableInspectionItem]:
        """Tables skipped during plan construction."""
        return [t for t in self.tables if t.is_skipped]


@dataclass
class TableInspectionResult:
    """Outcome of inspecting an individual table."""

    table_name: str
    schema_name: Optional[str] = None
    display_name: str = ""
    is_skipped: bool = False
    skip_reason: Optional[str] = None
    query_result: Optional[QueryResult] = None
    is_empty: bool = False
    is_error: bool = False
    error_message: Optional[str] = None
    duration_ms: float = 0.0

    def __post_init__(self) -> None:
        if not self.display_name:
            if self.schema_name:
                self.display_name = f"{self.schema_name}.{self.table_name}"
            else:
                self.display_name = self.table_name


@dataclass
class MultiTableInspectionResult:
    """Aggregate outcome of multi-table inspection."""

    plan: MultiTableInspectionPlan
    table_results: list[TableInspectionResult] = field(default_factory=list)
    total_inspected: int = 0
    tables_with_rows: int = 0
    empty_tables: int = 0
    skipped_tables: int = 0
    failed_tables: int = 0
    total_rows: int = 0
    total_duration_ms: float = 0.0


def compute_plan_hash(
    profile_name: str,
    dialect_val: str,
    row_limit: int,
    items: list[TableInspectionItem],
) -> str:
    """Generate SHA-256 fingerprint of the inspection plan to guarantee immutability."""
    raw = f"{profile_name}:{dialect_val}:{row_limit}:" + ";".join(
        f"{it.display_name}:{it.is_skipped}:{it.sql or ''}" for it in items
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _get_sqlglot_dialect(dialect: Dialect) -> str:
    """Map support Dialect to SQLGlot dialect string."""
    if dialect == Dialect.POSTGRES:
        return "postgres"
    elif dialect == Dialect.MYSQL:
        return "mysql"
    elif dialect == Dialect.SQLITE:
        return "sqlite"
    elif dialect == Dialect.MSSQL:
        return "tsql"
    return "postgres"


def build_inspection_plan(
    profile: ConnectionProfile,
    schema: DatabaseSchema,
    target_tables: Optional[list[str]] = None,
    row_limit: int = 1,
    max_tables: Optional[int] = None,
    user_input_func: Callable[[str], str] = input,
) -> Optional[MultiTableInspectionPlan]:
    """Construct a verified, deterministic inspection plan with safe single-table queries.

    Args:
        profile: Active database connection profile.
        schema: Discovered database schema metadata.
        target_tables: Optional subset of table names requested by user.
        row_limit: Max rows per table (default 1).
        max_tables: Maximum overview cap before asking user.
        user_input_func: Callback for user prompts.

    Returns:
        MultiTableInspectionPlan if successfully built and accepted, or None if declined.
    """
    if profile.dialect == Dialect.MONGODB:
        return None

    cap = max_tables or int(os.getenv("SUPPORT_MAX_INSPECTION_TABLES", str(DEFAULT_MAX_INSPECTION_TABLES)))
    bounded_limit = max(1, min(row_limit, 50))
    sqlglot_dialect = _get_sqlglot_dialect(profile.dialect)

    # 1. Resolve tables to inspect
    available_tables = schema.tables
    all_table_names = sorted(list(available_tables.keys()))
    total_db_tables = len(all_table_names)
    is_capped = False

    if target_tables:
        # Match user-specified tables case-insensitively or via schema.get_table
        lookup = {t.lower(): t for t in all_table_names}
        selected_names: list[str] = []
        for req in target_tables:
            req_clean = req.strip()
            resolved_tbl = schema.get_table(req_clean)
            if resolved_tbl:
                canonical = resolved_tbl.full_name
                if canonical not in selected_names:
                    selected_names.append(canonical)
            elif req_clean.lower() in lookup:
                canonical = lookup[req_clean.lower()]
                if canonical not in selected_names:
                    selected_names.append(canonical)
            else:
                selected_names.append(req_clean)
    else:
        # All tables requested
        if total_db_tables > cap:
            console.print()
            console.print(
                f"[bold yellow]Notice:[/bold yellow] The database '{profile.name}' contains [bold white]{total_db_tables}[/bold white] tables, "
                f"which exceeds the default overview limit of {cap}."
            )
            try:
                ans = user_input_func(f"Inspect the first {cap} tables? [y/N]: ").strip().lower()
            except (KeyboardInterrupt, EOFError):
                console.print("\n[dim]Multi-table inspection cancelled.[/dim]\n")
                return None

            if ans not in ("y", "yes"):
                console.print("[dim]Inspection cancelled. Please specify a smaller set of tables.[/dim]\n")
                return None

            selected_names = all_table_names[:cap]
            is_capped = True
        else:
            selected_names = list(all_table_names)

    # 2. Build safe individual query for each table
    items: list[TableInspectionItem] = []
    for t_name in selected_names:
        table_schema: Optional[TableSchema] = available_tables.get(t_name)
        if not table_schema:
            table_schema = schema.get_table(t_name)

        if not table_schema:
            items.append(
                TableInspectionItem(
                    table_name=t_name,
                    display_name=t_name,
                    is_skipped=True,
                    skip_reason=f"Table '{t_name}' not found in database schema",
                )
            )
            continue

        # Filter sensitive columns using centralized privacy policy
        safe_cols = [c.name for c in table_schema.columns if not is_sensitive_column_name(c.name)]
        sens_cols = [c.name for c in table_schema.columns if is_sensitive_column_name(c.name)]

        if not safe_cols:
            # Table has no safe non-sensitive columns to display
            sens_desc = ", ".join(sens_cols) if sens_cols else "sensitive credentials"
            items.append(
                TableInspectionItem(
                    table_name=table_schema.name,
                    schema_name=table_schema.schema_name,
                    display_name=table_schema.full_name,
                    sensitive_columns_excluded=sens_cols,
                    is_skipped=True,
                    skip_reason=f"Table contains only sensitive column(s): {sens_desc}",
                )
            )
            continue

        # Construct safe single-table SELECT statement with dialect-aware quoting
        quoted_cols = [exp.to_identifier(c, quoted=True) for c in safe_cols]
        if table_schema.schema_name:
            quoted_table = exp.Table(
                this=exp.to_identifier(table_schema.name, quoted=True),
                db=exp.to_identifier(table_schema.schema_name, quoted=True),
            )
        else:
            quoted_table = exp.Table(
                this=exp.to_identifier(table_schema.name, quoted=True),
            )
        sql_query = exp.select(*quoted_cols).from_(quoted_table).limit(bounded_limit).sql(dialect=sqlglot_dialect) + ";"

        # Validate with existing single-statement SQL validator
        val_res = validate_sql_query(sql_query, dialect=profile.dialect, schema=schema)
        if not val_res.is_valid:
            items.append(
                TableInspectionItem(
                    table_name=table_schema.name,
                    schema_name=table_schema.schema_name,
                    display_name=table_schema.full_name,
                    columns=safe_cols,
                    sensitive_columns_excluded=sens_cols,
                    is_skipped=True,
                    skip_reason=f"Validation failed: {val_res.status_message}",
                )
            )
            continue

        items.append(
            TableInspectionItem(
                table_name=table_schema.name,
                schema_name=table_schema.schema_name,
                display_name=table_schema.full_name,
                columns=safe_cols,
                sensitive_columns_excluded=sens_cols,
                sql=sql_query,
                is_skipped=False,
            )
        )

    plan_hash = compute_plan_hash(profile.name, profile.dialect.value, bounded_limit, items)

    return MultiTableInspectionPlan(
        database=profile.name,
        dialect=profile.dialect,
        row_limit=bounded_limit,
        tables=items,
        total_database_tables=total_db_tables,
        is_capped=is_capped,
        cap_limit=cap,
        plan_hash=plan_hash,
    )


def display_inspection_preview(
    plan: MultiTableInspectionPlan,
    timeout_seconds: float = 10.0,
) -> None:
    """Render concise multi-table inspection preview panel before approval."""
    console.print()
    console.print(
        f"[bold cyan]Multi-Table Inspection Preview[/bold cyan] · [dim]{plan.database} ({plan.dialect.value})[/dim]"
    )
    console.print("[dim]------------------------------------------------------------[/dim]")
    console.print(f"  [bold white]Database:[/bold white]      {plan.database} ({plan.dialect.value})")

    eligible_count = len(plan.eligible_tables)
    skipped_count = len(plan.skipped_tables)
    skip_note = f" ({skipped_count} skipped: no safe columns or not in schema)" if skipped_count else ""
    cap_note = f" [yellow](capped at {plan.cap_limit} of {plan.total_database_tables} tables)[/yellow]" if plan.is_capped else ""
    console.print(f"  [bold white]Scope:[/bold white]         {eligible_count} table(s) to sample{skip_note}{cap_note}")
    console.print(f"  [bold white]Row Limit:[/bold white]     {plan.row_limit} row(s) per table")

    # Sensitive columns note
    total_excluded = sum(len(t.sensitive_columns_excluded) for t in plan.tables)
    sens_note = f" · {total_excluded} sensitive column(s) excluded" if total_excluded else ""
    timeout_str = format_timeout(timeout_seconds)
    console.print(
        f"  [bold white]Safeguards:[/bold white]    Read-only enforced · Timeout: {timeout_str}{sens_note}"
    )

    if plan.skipped_tables:
        console.print()
        console.print("[bold yellow]Skipped Tables:[/bold yellow]")
        for st in plan.skipped_tables:
            console.print(f"  [dim]•[/dim] [white]{st.display_name}[/white]: [dim]{st.skip_reason}[/dim]")

    console.print("[dim]------------------------------------------------------------[/dim]")


def execute_multi_table_inspection(
    plan: MultiTableInspectionPlan,
    profile: ConnectionProfile,
    state: WorkspaceSessionState,
    db_service: Optional[DatabaseService] = None,
    executor: Optional[ReadOnlyQueryExecutor] = None,
    user_input_func: Callable[[str], str] = input,
    timeout_seconds: float = 10.0,
    store: Optional[QueryHistoryStore] = None,
    original_question: str = "",
) -> Optional[MultiTableInspectionResult]:
    """Execute multi-table inspection with explicit approval, independent execution, and result rendering.

    Args:
        plan: Verified inspection plan.
        profile: Target database profile.
        state: Active workspace session state.
        db_service: Optional database service.
        executor: Optional read-only executor instance.
        user_input_func: Callback for user prompts.
        timeout_seconds: Query timeout per table.
        store: Optional query history store.
        original_question: User's original natural-language query.

    Returns:
        MultiTableInspectionResult if approved and executed, None if cancelled.
    """
    dbs = db_service or DatabaseService()
    exec_inst = executor or ReadOnlyQueryExecutor()
    hist_store = store or QueryHistoryStore()

    eligible = plan.eligible_tables
    if not eligible and not plan.skipped_tables:
        console.print("[yellow]No accessible tables found to inspect.[/yellow]\n")
        return None

    # 1. Display preview
    display_inspection_preview(plan, timeout_seconds=timeout_seconds)

    if not eligible:
        console.print("[yellow]No eligible tables to query (all requested tables were skipped).[/yellow]\n")
        return None

    # 2. Ask for explicit user approval (Enter defaults to No)
    try:
        raw_ans = user_input_func(
            f"Sample {plan.row_limit} row(s) from {len(eligible)} table(s) in '{profile.name}'? [y/N]: "
        )
    except (KeyboardInterrupt, EOFError):
        console.print("\n[dim]Multi-table inspection cancelled. No queries were executed.[/dim]\n")
        return None

    ans = raw_ans.strip().lower()
    if ans not in ("y", "yes"):
        console.print("[dim]Multi-table inspection cancelled. No queries were executed.[/dim]\n")
        return None

    # 3. Plan immutability check: ensure plan was not modified since approval
    current_hash = compute_plan_hash(profile.name, profile.dialect.value, plan.row_limit, plan.tables)
    if current_hash != plan.plan_hash:
        console.print("[bold red]Plan modified since approval. Inspection aborted for safety.[/bold red]\n")
        return None

    # Check stale database selection
    if state.active_database != profile.name:
        console.print(
            f"[bold red]Stale inspection plan:[/bold red] Plan was generated for '{plan.database}', "
            f"but active database is now '{state.active_database}'.\n"
        )
        return None

    # 4. Fetch database password
    password: Optional[str] = None
    try:
        _, password = dbs.manager.get_profile_with_password(profile.name)
    except Exception:
        pass

    cfg = ExecutionConfig(max_rows=plan.row_limit, timeout_seconds=timeout_seconds)
    results: list[TableInspectionResult] = []
    total_returned_rows = 0
    tables_with_rows = 0
    empty_tables = 0
    skipped_tables = 0
    failed_tables = 0
    start_all = time.perf_counter()

    console.print()
    console.print(f"[bold cyan]Inspecting Tables in '{profile.name}'...[/bold cyan]")

    # 5. Execute each table independently
    for item in plan.tables:
        if item.is_skipped or not item.sql:
            results.append(
                TableInspectionResult(
                    table_name=item.table_name,
                    schema_name=item.schema_name,
                    display_name=item.display_name,
                    is_skipped=True,
                    skip_reason=item.skip_reason,
                )
            )
            skipped_tables += 1
            console.print()
            console.print(f"[bold white]Table: {item.display_name}[/bold white] · [dim yellow]Skipped ({item.skip_reason})[/dim yellow]")
            continue

        console.print()
        console.print(f"[bold cyan]Table:[/bold cyan] [bold white]{item.display_name}[/bold white]")

        t_start = time.perf_counter()
        q_res: QueryResult = exec_inst.execute(
            profile=profile,
            password=password,
            sql=item.sql,
            config=cfg,
        )
        t_dur_ms = (time.perf_counter() - t_start) * 1000.0

        if not q_res.is_success:
            failed_tables += 1
            # Display error without leaking sensitive credentials
            clean_err = q_res.error_message or "Query execution failed"
            if password and password in clean_err:
                clean_err = clean_err.replace(password, "***")

            results.append(
                TableInspectionResult(
                    table_name=item.table_name,
                    schema_name=item.schema_name,
                    display_name=item.display_name,
                    is_error=True,
                    error_message=clean_err,
                    duration_ms=t_dur_ms,
                    query_result=q_res,
                )
            )
            console.print(f"  [bold red]Execution Failed:[/bold red] {clean_err}")
            console.print(f"  [dim]Duration: {format_duration(duration_ms=t_dur_ms)}[/dim]")
        elif q_res.row_count == 0:
            empty_tables += 1
            results.append(
                TableInspectionResult(
                    table_name=item.table_name,
                    schema_name=item.schema_name,
                    display_name=item.display_name,
                    is_empty=True,
                    duration_ms=t_dur_ms,
                    query_result=q_res,
                )
            )
            console.print("  [yellow]Table is empty (returned 0 rows).[/yellow]")
            console.print(f"  [dim]Completed in {format_duration(duration_ms=t_dur_ms)}[/dim]")
        else:
            tables_with_rows += 1
            total_returned_rows += q_res.row_count
            results.append(
                TableInspectionResult(
                    table_name=item.table_name,
                    schema_name=item.schema_name,
                    display_name=item.display_name,
                    duration_ms=t_dur_ms,
                    query_result=q_res,
                )
            )
            display_query_result(q_res, view_mode="auto")

    total_duration_ms = (time.perf_counter() - start_all) * 1000.0
    total_inspected = len(plan.tables)

    # 6. Concise Outcome Summary
    # Example: Inspected 12 tables · 10 returned rows · 1 empty table · 1 skipped
    parts = [f"Inspected {total_inspected} table{'s' if total_inspected != 1 else ''}"]
    parts.append(f"{tables_with_rows} returned rows" if tables_with_rows != 1 else "1 returned row")
    if empty_tables > 0:
        parts.append(f"{empty_tables} empty table{'s' if empty_tables != 1 else ''}")
    if skipped_tables > 0:
        parts.append(f"{skipped_tables} skipped")
    if failed_tables > 0:
        parts.append(f"{failed_tables} failed")

    summary_text = " · ".join(parts)
    console.print()
    console.print(f"[bold green]✓[/bold green] {summary_text} [dim]({format_duration(duration_ms=total_duration_ms)})[/dim]\n")

    # 7. Record in History without storing result rows
    history_sql = "\n".join(
        f"-- Table: {it.display_name}\n{it.sql}" for it in plan.eligible_tables if it.sql
    )
    hist_store.add_entry(
        question=original_question or f"Inspect tables in {profile.name}",
        sql=history_sql,
        database=profile.name,
        dialect=profile.dialect.value,
        explanation=f"Multi-table inspection of {total_inspected} table(s) in {profile.name} (limit {plan.row_limit} row(s) per table).",
        is_validated=True,
        validation_status="Multi-table inspection",
        is_schema_verified=True,
        schema_status=f"{total_inspected} tables inspected",
        execution_status=summary_text,
        duration_ms=total_duration_ms,
        row_count=total_returned_rows,
        error=f"{failed_tables} table(s) failed" if failed_tables else None,
        entry_type="multi_table_inspection",
    )

    # 8. Record in state assistant history (metadata only, no raw result rows)
    state.add_message(
        "assistant",
        f"Multi-table inspection complete: {summary_text}.",
        metadata={
            "intent": "multi_table_inspection",
            "tables_inspected": total_inspected,
            "row_count": total_returned_rows,
            "empty_tables": empty_tables,
            "skipped_tables": skipped_tables,
            "failed_tables": failed_tables,
            "duration_ms": total_duration_ms,
        },
    )

    return MultiTableInspectionResult(
        plan=plan,
        table_results=results,
        total_inspected=total_inspected,
        tables_with_rows=tables_with_rows,
        empty_tables=empty_tables,
        skipped_tables=skipped_tables,
        failed_tables=failed_tables,
        total_rows=total_returned_rows,
        total_duration_ms=total_duration_ms,
    )
