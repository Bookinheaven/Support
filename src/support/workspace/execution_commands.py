"""Command handler for explicitly approved, controlled read-only query execution."""

from typing import Callable, Optional

from support.cli.display import console
from support.core.dialect import Dialect
from support.core.formatting import format_duration
from support.db.service import DatabaseService
from support.query.display import display_execution_preview, display_query_result
from support.query.executor import ExecutionConfig, QueryResult, ReadOnlyQueryExecutor
from support.query.models import QueryDraft
from support.query.validator import validate_sql_query
from support.workspace.state import WorkspaceSessionState


def _estimate_affected_rows(draft: "QueryDraft", state: WorkspaceSessionState, dbs: DatabaseService) -> Optional[int]:
    """Safely derive or estimate affected row count for a mutation."""
    if getattr(draft, "is_readonly", True):
        return None

    target_tbl = getattr(draft, "target_table", None)
    if not target_tbl or not state.database_profile:
        return getattr(draft, "mutation_limit", None)

    try:
        from support.db.connection import DatabaseConnectionFactory
        from sqlalchemy import text
        password: Optional[str] = None
        try:
            _, password = dbs.manager.get_profile_with_password(state.active_database)
        except Exception:
            pass
        engine = DatabaseConnectionFactory.create_engine(
            state.database_profile,
            password=password,
            timeout=2,
            readonly=True,
        )
        with engine.connect() as conn:
            if getattr(draft, "is_unrestricted", False):
                count_res = conn.execute(text(f"SELECT COUNT(*) FROM {target_tbl}"))
                val = count_res.scalar()
                engine.dispose()
                return int(val) if val is not None else None
            elif getattr(draft, "selection_criteria", None):
                crit = draft.selection_criteria
                count_res = conn.execute(text(f"SELECT COUNT(*) FROM {target_tbl} WHERE {crit}"))
                val = count_res.scalar()
                engine.dispose()
                if val is not None:
                    count_val = int(val)
                    if getattr(draft, "is_bounded", False) and getattr(draft, "mutation_limit", None) is not None:
                        return min(count_val, draft.mutation_limit)
                    return count_val
        engine.dispose()
    except Exception:
        pass

    return getattr(draft, "mutation_limit", None)


def handle_run_command(
    state: WorkspaceSessionState,
    db_service: Optional[DatabaseService] = None,
    user_input_func: Callable[[str], str] = input,
    executor: Optional[ReadOnlyQueryExecutor] = None,
    config: Optional[ExecutionConfig] = None,
    view_mode: str = "table",
    llm_service: Optional[object] = None,
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
    if not draft or not draft.sql or getattr(draft, "is_clarification", False):
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
    reval = validate_sql_query(
        draft.sql,
        dialect=state.database_profile.dialect,
        schema=state.cached_schema,
        allow_mutations=not getattr(draft, "is_readonly", True),
    )
    if not reval.is_valid:
        console.print(f"[bold red]Pre-execution revalidation failed:[/bold red] {reval.status_message}\n")
        return False

    if reval.is_schema_verified is False or reval.unknown_tables:
        console.print(f"[bold red]Pre-execution schema verification failed:[/bold red] {reval.schema_status}\n")
        return False

    # 8. Display execution preview
    affected_estimate = _estimate_affected_rows(draft, state, dbs)
    display_execution_preview(
        draft=draft,
        profile_name=state.active_database,
        dialect_name=state.database_profile.dialect.value,
        config=cfg,
        affected_row_estimate=affected_estimate,
    )

    # 9. Explicit user approval defaulting to No
    if getattr(draft, "is_unrestricted", False):
        prompt_label = f"Approve FULL-TABLE {reval.operation} affecting ALL records in '{draft.target_table}'? [y/N]: "
    else:
        prompt_label = f"Approve execution of {reval.operation}? [y/N]: "

    try:
        raw_answer = user_input_func(prompt_label)
    except (KeyboardInterrupt, EOFError):
        console.print("\n[yellow]Execution cancelled. No queries were executed.[/yellow]\n")
        return False

    answer = raw_answer.strip().lower()
    if answer not in ("y", "yes"):
        console.print("[yellow]Execution cancelled. No queries were executed.[/yellow]\n")
        return False

    # 10. Execute: if non-read-only (DDL or Mutation), execute within transaction
    if not reval.is_readonly:
        import time
        from sqlalchemy import text
        from support.db.connection import DatabaseConnectionFactory

        start_t = time.perf_counter()
        password: Optional[str] = None
        try:
            _, password = dbs.manager.get_profile_with_password(state.active_database)
        except Exception:
            pass

        with console.status(f"[cyan]Executing {reval.operation}...[/cyan]", spinner="dots"):
            try:
                from support.core.performance import get_current_tracker
                tracker = get_current_tracker()
                t_conn0 = time.perf_counter()
                engine = DatabaseConnectionFactory.create_engine(
                    state.database_profile,
                    password=password,
                    timeout=10,
                    readonly=False,
                )
                with engine.connect() as conn:
                    if tracker:
                        tracker.stage_durations["db_connection"] = (
                            tracker.stage_durations.get("db_connection", 0.0) + (time.perf_counter() - t_conn0) * 1000.0
                        )
                    with conn.begin():
                        t_exec0 = time.perf_counter()
                        exec_res = conn.execute(text(draft.sql))
                        affected = exec_res.rowcount if hasattr(exec_res, "rowcount") and exec_res.rowcount != -1 else 0
                        if tracker:
                            tracker.stage_durations["db_execution"] = (
                                tracker.stage_durations.get("db_execution", 0.0) + (time.perf_counter() - t_exec0) * 1000.0
                            )
                engine.dispose()
                duration = (time.perf_counter() - start_t) * 1000.0
                try:
                    from support.query.schema_context import inspect_database_schema
                    state.cached_schema = inspect_database_schema(state.database_profile, db_service=dbs)
                except Exception:
                    pass
                console.print(f"\n[bold green]{reval.operation} executed successfully.[/bold green] ({affected} row(s) affected in {duration:.1f}ms)\n")
                draft.execution_status = f"Executed ({reval.operation}: {affected} rows in {duration:.1f}ms)"
                return True
            except Exception as e:
                console.print(f"\n[bold red]Execution failed:[/bold red] {e}\n")
                draft.execution_status = f"Failed ({e})"
                failed_res = QueryResult(
                    executed_sql=draft.sql,
                    error_message=str(e),
                    duration_ms=(time.perf_counter() - start_t) * 1000.0,
                )
                _record_execution_in_history(draft, failed_res, state)
                _handle_post_execution_recovery(
                    failed_draft=draft,
                    result=failed_res,
                    state=state,
                    dbs=dbs,
                    exec_inst=exec_inst,
                    cfg=cfg,
                    password=password,
                    user_input_func=user_input_func,
                    view_mode=view_mode,
                    llm_service=llm_service,
                )
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
        draft.execution_status = f"Executed ({result.row_count} rows in {format_duration(duration_ms=result.duration_ms)})"
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

    _record_execution_in_history(draft, result, state)

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
        or getattr(draft, "is_clarification", False)
        or not draft.is_validated
        or draft.unknown_tables
    ):
        return False

    # Pre-execution revalidation
    reval = validate_sql_query(
        draft.sql,
        dialect=state.database_profile.dialect,
        schema=state.cached_schema,
        allow_mutations=not getattr(draft, "is_readonly", True),
    )
    if not reval.is_valid:
        console.print(f"[bold red]Pre-execution revalidation failed:[/bold red] {reval.status_message}\n")
        return False

    if reval.is_schema_verified is False or reval.unknown_tables:
        console.print(f"[bold red]Pre-execution schema verification failed:[/bold red] {reval.schema_status}\n")
        return False

    # Prompt user for explicit approval
    affected_estimate = None
    if not reval.is_readonly:
        from support.query.display import display_mutation_preview
        affected_estimate = _estimate_affected_rows(draft, state, dbs)
        display_mutation_preview(
            draft=draft,
            profile_name=state.active_database,
            dialect_name=state.database_profile.dialect.value,
            affected_row_estimate=affected_estimate,
        )
        if getattr(draft, "is_unrestricted", False):
            prompt_str = f"Approve FULL-TABLE {reval.operation} affecting ALL records in '{draft.target_table}'? [y/N]: "
        else:
            prompt_str = "Approve execution? [y/N]: "
    else:
        prompt_str = "Execute this read-only query? [y/N]: "

    try:
        raw_answer = user_input_func(prompt_str)
    except (KeyboardInterrupt, EOFError):
        console.print("\n[dim]Execution cancelled. Draft retained (use /run to execute later).[/dim]\n")
        return False

    answer = raw_answer.strip().lower()
    if answer not in ("y", "yes"):
        console.print("[dim]Execution cancelled. Draft retained (use /run to execute later).[/dim]\n")
        return False

    # If non-read-only (DDL or Mutation), execute inside transaction
    if not reval.is_readonly:
        import time
        from sqlalchemy import text
        from support.db.connection import DatabaseConnectionFactory

        start_t = time.perf_counter()
        password: Optional[str] = None
        try:
            _, password = dbs.manager.get_profile_with_password(state.active_database)
        except Exception:
            pass

        with console.status(f"[cyan]Executing {reval.operation}...[/cyan]", spinner="dots"):
            try:
                from support.core.performance import get_current_tracker
                tracker = get_current_tracker()
                t_conn0 = time.perf_counter()
                engine = DatabaseConnectionFactory.create_engine(
                    state.database_profile,
                    password=password,
                    timeout=10,
                    readonly=False,
                )
                with engine.connect() as conn:
                    if tracker:
                        tracker.stage_durations["db_connection"] = (
                            tracker.stage_durations.get("db_connection", 0.0) + (time.perf_counter() - t_conn0) * 1000.0
                        )
                    with conn.begin():
                        t_exec0 = time.perf_counter()
                        exec_res = conn.execute(text(draft.sql))
                        affected = exec_res.rowcount if hasattr(exec_res, "rowcount") and exec_res.rowcount != -1 else 0
                        if tracker:
                            tracker.stage_durations["db_execution"] = (
                                tracker.stage_durations.get("db_execution", 0.0) + (time.perf_counter() - t_exec0) * 1000.0
                            )
                engine.dispose()
                duration = (time.perf_counter() - start_t) * 1000.0
                try:
                    from support.query.schema_context import inspect_database_schema
                    state.cached_schema = inspect_database_schema(state.database_profile, db_service=dbs)
                except Exception:
                    pass
                console.print(f"\n[bold green]{reval.operation} executed successfully.[/bold green] ({affected} row(s) affected in {duration:.1f}ms)\n")
                draft.execution_status = f"Executed ({reval.operation}: {affected} rows in {duration:.1f}ms)"
                state.add_message("assistant", f"Executed {reval.operation} on {state.active_database}: {draft.sql}")
                return True
            except Exception as e:
                console.print(f"\n[bold red]Execution failed:[/bold red] {e}\n")
                draft.execution_status = f"Failed ({e})"
                failed_res = QueryResult(
                    executed_sql=draft.sql,
                    error_message=str(e),
                    duration_ms=(time.perf_counter() - start_t) * 1000.0,
                )
                _record_execution_in_history(draft, failed_res, state)
                _handle_post_execution_recovery(
                    failed_draft=draft,
                    result=failed_res,
                    state=state,
                    dbs=dbs,
                    exec_inst=exec_inst,
                    cfg=cfg,
                    password=password,
                    user_input_func=user_input_func,
                    view_mode=view_mode,
                    llm_service=llm_service,
                )
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
        draft.execution_status = f"Executed ({result.row_count} rows in {format_duration(duration_ms=result.duration_ms)})"
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

    _record_execution_in_history(draft, result, state)

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


def _record_execution_in_history(
    draft: QueryDraft, result: QueryResult, state: WorkspaceSessionState
) -> None:
    """Record or update execution outcome in persistent query history."""
    try:
        from support.history.store import QueryHistoryStore

        store = QueryHistoryStore()
        updated = store.update_last_execution(
            sql=draft.sql,
            execution_status=draft.execution_status or "Executed",
            duration_ms=result.duration_ms,
            row_count=result.row_count,
            error=result.error_message,
        )
        if not updated:
            store.add_entry(
                question=draft.question,
                sql=draft.sql,
                database=state.active_database or "unknown",
                dialect=draft.dialect,
                explanation=draft.explanation,
                is_validated=draft.is_validated,
                validation_status=draft.validation_status,
                is_schema_verified=draft.is_schema_verified,
                schema_status=draft.schema_status,
                execution_status=draft.execution_status,
                duration_ms=result.duration_ms,
                row_count=result.row_count,
                error=result.error_message,
            )
    except Exception:
        pass


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

    if getattr(failed_draft, "post_exec_repaired", False):
        return

    from support.query.generator import QueryGenerator
    generator = QueryGenerator(db_service=dbs, llm_service=llm_service)

    with console.status("[cyan]Attempting schema-grounded query correction...[/cyan]", spinner="dots"):
        repaired = generator.repair_execution_failure(failed_draft, result.error_message, state)

    if (
        repaired
        and repaired.sql
        and repaired.is_validated
        and repaired.is_schema_verified is not False
        and not repaired.unknown_tables
    ):
        repaired.post_exec_repaired = True
        console.print("\n[bold yellow]Previous execution failed. Proposed corrected query has NOT been executed.[/bold yellow]")
        from support.query.display import display_query_draft
        display_query_draft(
            repaired,
            db_name=state.active_database,
            dialect_name=state.database_profile.dialect.value if state.database_profile else None,
        )
        state.last_query_draft = repaired

        op_name = repaired.operation or "query"
        prompt_str = "Execute this corrected read-only query? [y/N]: " if repaired.is_readonly else f"Execute this corrected {op_name}? [y/N]: "
        try:
            corr_answer = user_input_func(prompt_str)
        except (KeyboardInterrupt, EOFError):
            console.print("\n[dim]Corrected query not executed. Draft retained (use /run to execute later).[/dim]\n")
            return

        if corr_answer.strip().lower() in ("y", "yes"):
            # Fresh pre-execution revalidation
            if state.database_profile:
                reval = validate_sql_query(
                    repaired.sql,
                    dialect=state.database_profile.dialect,
                    schema=state.cached_schema,
                    allow_mutations=not getattr(repaired, "is_readonly", True),
                    expected_operation=repaired.operation,
                )
                if not reval.is_valid or reval.is_schema_verified is False or reval.unknown_tables:
                    console.print(f"[bold red]Pre-execution revalidation failed on corrected query:[/bold red] {reval.status_message}\n")
                    return

            if not reval.is_readonly:
                import time
                from sqlalchemy import text
                from support.db.connection import DatabaseConnectionFactory

                start_t = time.perf_counter()
                with console.status(f"[cyan]Executing {reval.operation}...[/cyan]", spinner="dots"):
                    try:
                        engine = DatabaseConnectionFactory.create_engine(
                            state.database_profile,
                            password=password,
                            timeout=10,
                            readonly=False,
                        )
                        with engine.connect() as conn:
                            with conn.begin():
                                exec_res = conn.execute(text(repaired.sql))
                                affected = exec_res.rowcount if hasattr(exec_res, "rowcount") and exec_res.rowcount != -1 else 0
                        engine.dispose()
                        duration = (time.perf_counter() - start_t) * 1000.0
                        try:
                            from support.query.schema_context import inspect_database_schema
                            state.cached_schema = inspect_database_schema(state.database_profile, db_service=dbs)
                        except Exception:
                            pass
                        console.print(f"\n[bold green]{reval.operation} executed successfully.[/bold green] ({affected} row(s) affected in {duration:.1f}ms)\n")
                        repaired.execution_status = f"Executed ({reval.operation}: {affected} rows in {duration:.1f}ms)"
                        state.add_message("assistant", f"Executed {reval.operation} on {state.active_database}: {repaired.sql}")
                        _record_execution_in_history(
                            repaired,
                            QueryResult(executed_sql=repaired.sql, row_count=affected, duration_ms=duration),
                            state,
                        )
                        return
                    except Exception as e:
                        console.print(f"\n[bold red]Execution failed:[/bold red] {e}\n")
                        repaired.execution_status = f"Failed ({e})"
                        _record_execution_in_history(
                            repaired,
                            QueryResult(executed_sql=repaired.sql, error_message=str(e)),
                            state,
                        )
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
                repaired.execution_status = f"Executed ({corr_res.row_count} rows in {format_duration(duration_ms=corr_res.duration_ms)})"
            else:
                repaired.execution_status = f"Failed ({corr_res.error_message})"
            _record_execution_in_history(repaired, corr_res, state)
        else:
            console.print("[dim]Corrected query not executed. Draft retained (use /run to execute later).[/dim]\n")
    else:
        console.print("[yellow]Could not automatically repair query with available schema.[/yellow]\n")

