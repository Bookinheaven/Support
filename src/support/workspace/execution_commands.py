"""Command handler for explicitly approved, controlled read-only query execution."""

from typing import Callable, Optional

from support.cli.display import console
from support.core.dialect import Dialect
from support.db.service import DatabaseService
from support.query.display import display_execution_preview, display_query_result
from support.query.executor import ExecutionConfig, QueryResult, ReadOnlyQueryExecutor
from support.query.validator import validate_sql_query
from support.workspace.state import WorkspaceSessionState


def handle_run_command(
    state: WorkspaceSessionState,
    db_service: Optional[DatabaseService] = None,
    user_input_func: Callable[[str], str] = input,
    executor: Optional[ReadOnlyQueryExecutor] = None,
    config: Optional[ExecutionConfig] = None,
    view_mode: str = "table",
) -> bool:
    """Execute the most recent eligible query draft with explicit confirmation and safeguards.

    Returns:
        False to indicate the interactive workspace session should continue.
    """
    dbs = db_service or DatabaseService()
    exec_inst = executor or ReadOnlyQueryExecutor()
    cfg = config or ExecutionConfig()

    # 1. Require active database
    if not state.active_database or not state.database_profile:
        console.print("[bold yellow]No active database selected.[/bold yellow]")
        console.print("[dim]Use [bold white]/db[/bold white] to select an active database first.[/dim]\n")
        return False

    # 2. Block MongoDB
    if state.database_profile.dialect == Dialect.MONGODB:
        console.print("[bold yellow]MongoDB query execution is not supported.[/bold yellow]")
        console.print("[dim]Only relational database query execution is supported.[/dim]\n")
        return False

    # 3. Require active draft
    draft = state.last_query_draft
    if not draft or not draft.sql:
        console.print("[bold yellow]No active query draft to execute.[/bold yellow]")
        console.print("[dim]Ask a natural-language question first to generate a query draft.[/dim]\n")
        return False

    # 3.5 If draft was already executed and view mode change is requested, use cached result
    clean_mode = view_mode.strip().lower()
    if (
        state.last_query_result is not None
        and draft.execution_status.startswith("Executed")
        and clean_mode in ("vertical", "v", "record", "wide", "table")
    ):
        console.print("[dim]Re-displaying existing query results without re-querying database.[/dim]")
        display_query_result(state.last_query_result, view_mode=view_mode)
        return False

    # 4. Dialect alignment check
    if draft.dialect != state.database_profile.dialect.value:
        console.print(
            f"[bold red]Draft dialect mismatch:[/bold red] Draft was generated for '{draft.dialect}', "
            f"but active database is '{state.database_profile.dialect.value}'. "
            f"[dim]Please generate a new draft for this database.[/dim]\n"
        )
        return False

    # 5. Structural/read-only validation check
    if not draft.is_validated:
        console.print(
            f"[bold red]Draft cannot be executed:[/bold red] Failed structural validation "
            f"([white]{draft.validation_status}[/white]).\n"
        )
        return False

    # 6. Schema alignment verification
    if draft.is_schema_verified is not True:
        console.print(
            f"[bold red]Draft cannot be executed:[/bold red] Schema alignment could not be verified "
            f"([white]{draft.schema_status}[/white]).\n"
        )
        return False

    if draft.unknown_tables:
        unknown_str = ", ".join(draft.unknown_tables)
        console.print(
            f"[bold red]Draft cannot be executed:[/bold red] References unknown tables: {unknown_str}.\n"
        )
        return False

    # 7. Pre-execution revalidation
    reval = validate_sql_query(draft.sql, dialect=state.database_profile.dialect, schema=state.cached_schema)
    if not reval.is_valid:
        console.print(f"[bold red]Pre-execution revalidation failed:[/bold red] {reval.status_message}\n")
        return False

    if reval.is_schema_verified is False or reval.unknown_tables:
        console.print(f"[bold red]Pre-execution schema verification failed:[/bold red] {reval.schema_status}\n")
        return False

    # 8. Display execution preview
    display_execution_preview(
        draft=draft,
        profile_name=state.active_database,
        dialect_name=state.database_profile.dialect.value,
        config=cfg,
    )

    # 9. Explicit user approval defaulting to No
    try:
        raw_answer = user_input_func("Approve execution? [y/N]: ")
    except (KeyboardInterrupt, EOFError):
        console.print("\n[yellow]Execution cancelled. No queries were executed.[/yellow]\n")
        return False

    answer = raw_answer.strip().lower()
    if answer not in ("y", "yes"):
        console.print("[yellow]Execution cancelled. No queries were executed.[/yellow]\n")
        return False

    # 10. Execute under read-only safeguards
    password: Optional[str] = None
    try:
        _, password = dbs.manager.get_profile_with_password(state.active_database)
    except Exception:
        pass

    with console.status("[cyan]Executing read-only query...[/cyan]", spinner="dots"):
        result: QueryResult = exec_inst.execute(
            profile=state.database_profile,
            password=password,
            sql=draft.sql,
            config=cfg,
        )

    state.last_query_result = result
    display_query_result(result, view_mode=view_mode)

    if result.is_success:
        draft.execution_status = f"Executed ({result.row_count} rows in {result.duration_ms:.1f}ms)"
    else:
        draft.execution_status = f"Failed ({result.error_message})"
        _handle_post_execution_recovery(
            failed_draft=draft,
            result=result,
            state=state,
            dbs=dbs,
            exec_inst=exec_inst,
            cfg=cfg,
            password=password,
            user_input_func=user_input_func,
            view_mode=view_mode,
        )

    state.add_message(
        "assistant",
        f"Executed query draft on {state.active_database}.",
        metadata={
            "sql": draft.sql,
            "row_count": result.row_count,
            "duration_ms": result.duration_ms,
            "is_truncated": result.is_truncated,
            "error": result.error_message,
        },
    )

    return False


def handle_conversational_execution(
    draft: QueryDraft,
    state: WorkspaceSessionState,
    db_service: Optional[DatabaseService] = None,
    llm_service: Optional[object] = None,
    user_input_func: Callable[[str], str] = input,
    executor: Optional[ReadOnlyQueryExecutor] = None,
    config: Optional[ExecutionConfig] = None,
    view_mode: str = "auto",
) -> bool:
    """Conversational approval and execution flow immediately after draft generation."""
    dbs = db_service or DatabaseService()
    exec_inst = executor or ReadOnlyQueryExecutor()
    cfg = config or ExecutionConfig()

    # Must have active database and non-mongodb profile
    if not state.active_database or not state.database_profile:
        return False
    if state.database_profile.dialect == Dialect.MONGODB:
        return False

    # Only eligible drafts are asked for execution
    if (
        not draft.sql
        or not draft.is_validated
        or draft.is_schema_verified is not True
        or draft.unknown_tables
    ):
        return False

    # Prompt user for explicit approval
    try:
        raw_answer = user_input_func("Execute this read-only query? [y/N]: ")
    except (KeyboardInterrupt, EOFError):
        console.print("\n[dim]Query not executed. Draft retained (use /run to execute later).[/dim]\n")
        return False

    answer = raw_answer.strip().lower()
    if answer not in ("y", "yes"):
        console.print("[dim]Query not executed. Draft retained (use /run to execute later).[/dim]\n")
        return False

    # Pre-execution revalidation
    reval = validate_sql_query(draft.sql, dialect=state.database_profile.dialect, schema=state.cached_schema)
    if not reval.is_valid:
        console.print(f"[bold red]Pre-execution revalidation failed:[/bold red] {reval.status_message}\n")
        return False

    if reval.is_schema_verified is False or reval.unknown_tables:
        console.print(f"[bold red]Pre-execution schema verification failed:[/bold red] {reval.schema_status}\n")
        return False

    password: Optional[str] = None
    try:
        _, password = dbs.manager.get_profile_with_password(state.active_database)
    except Exception:
        pass

    with console.status("[cyan]Executing read-only query...[/cyan]", spinner="dots"):
        result: QueryResult = exec_inst.execute(
            profile=state.database_profile,
            password=password,
            sql=draft.sql,
            config=cfg,
        )

    state.last_query_result = result
    display_query_result(result, view_mode=view_mode)

    if result.is_success:
        draft.execution_status = f"Executed ({result.row_count} rows in {result.duration_ms:.1f}ms)"
    else:
        draft.execution_status = f"Failed ({result.error_message})"
        _handle_post_execution_recovery(
            failed_draft=draft,
            result=result,
            state=state,
            dbs=dbs,
            exec_inst=exec_inst,
            cfg=cfg,
            password=password,
            user_input_func=user_input_func,
            view_mode=view_mode,
            llm_service=llm_service,
        )

    state.add_message(
        "assistant",
        f"Executed query draft on {state.active_database}.",
        metadata={
            "sql": draft.sql,
            "row_count": result.row_count,
            "duration_ms": result.duration_ms,
            "is_truncated": result.is_truncated,
            "error": result.error_message,
        },
    )

    return False


def _handle_post_execution_recovery(
    failed_draft: QueryDraft,
    result: QueryResult,
    state: WorkspaceSessionState,
    dbs: DatabaseService,
    exec_inst: ReadOnlyQueryExecutor,
    cfg: ExecutionConfig,
    password: Optional[str],
    user_input_func: Callable[[str], str],
    view_mode: str = "auto",
    llm_service: Optional[object] = None,
) -> None:
    """Perform at most 1 bounded schema-grounded recovery attempt on recoverable execution errors."""
    from support.query.repair import is_recoverable_execution_error

    if not is_recoverable_execution_error(result.error_message):
        console.print(
            "[dim]Execution error is not a recoverable SQL syntax or schema defect "
            "(database connectivity, permission, or timeout). Query will not be automatically rewritten.[/dim]\n"
        )
        return

    if failed_draft.repair_attempts >= 1:
        return

    from support.query.generator import QueryGenerator
    generator = QueryGenerator(db_service=dbs, llm_service=llm_service)

    with console.status("[cyan]Attempting schema-grounded query correction...[/cyan]", spinner="dots"):
        repaired = generator.repair_execution_failure(failed_draft, result.error_message, state)

    if (
        repaired
        and repaired.sql
        and repaired.is_validated
        and repaired.is_schema_verified is True
        and not repaired.unknown_tables
    ):
        console.print("\n[bold yellow]Previous execution failed. Proposed corrected query has NOT been executed.[/bold yellow]")
        from support.query.display import display_query_draft
        display_query_draft(
            repaired,
            db_name=state.active_database,
            dialect_name=state.database_profile.dialect.value if state.database_profile else None,
        )
        state.last_query_draft = repaired

        try:
            corr_answer = user_input_func("Execute this corrected read-only query? [y/N]: ")
        except (KeyboardInterrupt, EOFError):
            console.print("\n[dim]Corrected query not executed. Draft retained (use /run to execute later).[/dim]\n")
            return

        if corr_answer.strip().lower() in ("y", "yes"):
            # Fresh pre-execution revalidation
            if state.database_profile:
                reval = validate_sql_query(
                    repaired.sql, dialect=state.database_profile.dialect, schema=state.cached_schema
                )
                if not reval.is_valid or reval.is_schema_verified is False or reval.unknown_tables:
                    console.print(f"[bold red]Pre-execution revalidation failed on corrected query:[/bold red] {reval.status_message}\n")
                    return

            with console.status("[cyan]Executing corrected read-only query...[/cyan]", spinner="dots"):
                corr_res = exec_inst.execute(
                    profile=state.database_profile,
                    password=password,
                    sql=repaired.sql,
                    config=cfg,
                )
            state.last_query_result = corr_res
            display_query_result(corr_res, view_mode=view_mode)
            if corr_res.is_success:
                repaired.execution_status = f"Executed ({corr_res.row_count} rows in {corr_res.duration_ms:.1f}ms)"
            else:
                repaired.execution_status = f"Failed ({corr_res.error_message})"
        else:
            console.print("[dim]Corrected query not executed. Draft retained (use /run to execute later).[/dim]\n")
    else:
        console.print("[yellow]Could not automatically repair query with available schema.[/yellow]\n")

