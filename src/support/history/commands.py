"""Slash command handlers for query history (/history, /history <N>, /history use <N>, /history clear)."""

from typing import TYPE_CHECKING, Callable, Optional
from rich import box
from rich.syntax import Syntax
from rich.table import Table

from support.cli.display import console
from support.core.formatting import format_duration
from support.history.models import QueryHistoryEntry
from support.history.store import QueryHistoryStore
from support.query.display import display_query_draft
from support.query.models import QueryDraft
from support.query.validator import validate_sql_query

if TYPE_CHECKING:
    from support.workspace.state import WorkspaceSessionState


def handle_history_command(
    state: WorkspaceSessionState,
    store: Optional[QueryHistoryStore] = None,
    arg: str = "",
    user_input_func: Callable[[str], str] = input,
    db_service: Optional[object] = None,
    llm_service: Optional[object] = None,
) -> None:
    """Handle /history commands with listing, inspection, restoration, and clearing."""
    hist_store = store or QueryHistoryStore()

    if not hist_store.enabled:
        console.print("[yellow]Query history is disabled by configuration (SUPPORT_HISTORY_ENABLED=false).[/yellow]\n")
        return

    clean_arg = arg.strip()
    parts = clean_arg.split()

    if not clean_arg:
        _display_history_list(hist_store)
    elif clean_arg.lower() == "clear":
        _handle_history_clear(hist_store, user_input_func)
    elif len(parts) >= 2 and parts[0].lower() == "use" and parts[1].isdigit():
        _handle_history_use(
            index=int(parts[1]),
            state=state,
            store=hist_store,
            user_input_func=user_input_func,
            db_service=db_service,
            llm_service=llm_service,
        )
    elif clean_arg.isdigit():
        _display_history_entry(int(clean_arg), hist_store)
    else:
        console.print(f"[bold red]Unknown history subcommand:[/bold red] '{clean_arg}'.")
        console.print("[dim]Usage: /history, /history <N>, /history use <N>, or /history clear[/dim]\n")


def _display_history_list(store: QueryHistoryStore) -> None:
    """Render compact list of stored query history."""
    entries = store.list_entries()
    if not entries:
        console.print("[yellow]No query history recorded yet.[/yellow]")
        console.print("[dim]Questions asked during workspace sessions will appear here.[/dim]\n")
        return

    console.print()
    console.print("[bold cyan]Recent Query History[/bold cyan]")

    table = Table(box=box.SIMPLE, header_style="bold cyan", show_header=True, pad_edge=False)
    table.add_column("#", justify="right", style="dim", width=4)
    table.add_column("Question", style="bold white")
    table.add_column("Database", style="cyan", no_wrap=True)
    table.add_column("Status", justify="center", no_wrap=True)
    table.add_column("Time", style="dim", no_wrap=True)

    for idx, entry in enumerate(entries, start=1):
        q_text = entry.question.replace("\n", " ")
        if len(q_text) > 42:
            q_text = q_text[:39] + "..."

        db_text = f"{entry.database} ({entry.dialect})"

        # Concise status
        if entry.entry_type == "multi_table_inspection":
            stat_cell = "[bold cyan]Inspected[/bold cyan]"
        elif entry.execution_status and "Executed" in entry.execution_status:
            stat_cell = "[bold green]Executed[/bold green]"
        elif entry.execution_status and "Failed" in entry.execution_status:
            stat_cell = "[bold red]Failed[/bold red]"
        elif entry.is_validated:
            stat_cell = "[green]Drafted[/green]"
        else:
            stat_cell = "[yellow]Invalid[/yellow]"

        time_str = entry.timestamp.strftime("%Y-%m-%d %H:%M") if hasattr(entry.timestamp, "strftime") else str(entry.timestamp)[:16]

        table.add_row(str(idx), q_text, db_text, stat_cell, time_str)

    console.print(table)
    console.print("[dim]Use '/history <N>' to inspect details or '/history use <N>' to restore a draft.[/dim]\n")


def _display_history_entry(index: int, store: QueryHistoryStore) -> None:
    """Display detailed view of a single historical entry."""
    entry = store.get_entry(index)
    if not entry:
        console.print(f"[bold red]Error:[/bold red] History entry #{index} not found.")
        console.print("[dim]Use '/history' to see all available entries.[/dim]\n")
        return

    console.print()
    console.print(f"[bold cyan]Query History #{index}[/bold cyan] · [dim]{entry.database} ({entry.dialect})[/dim]")
    console.print("[dim]------------------------------------------------------------[/dim]")
    if entry.entry_type == "multi_table_inspection":
        console.print("  [bold white]Type:[/bold white]              [bold cyan]Multi-Table Inspection[/bold cyan]")
    console.print(f"  [bold white]Question:[/bold white]          {entry.question}")
    console.print(f"  [bold white]Database:[/bold white]          {entry.database} ({entry.dialect})")
    time_str = entry.timestamp.strftime("%Y-%m-%d %H:%M:%S UTC") if hasattr(entry.timestamp, "strftime") else str(entry.timestamp)
    console.print(f"  [bold white]Timestamp:[/bold white]         {time_str}")

    val_color = "green" if entry.is_validated else "red"
    console.print(f"  [bold white]Validation:[/bold white]        [{val_color}]{entry.validation_status}[/{val_color}]")

    if entry.schema_status:
        schema_color = "green" if entry.is_schema_verified else "yellow"
        console.print(f"  [bold white]Schema Status:[/bold white]     [{schema_color}]{entry.schema_status}[/{schema_color}]")

    if entry.execution_status:
        console.print(f"  [bold white]Execution Status:[/bold white]  {entry.execution_status}")

    if entry.duration_ms is not None:
        dur_str = format_duration(duration_ms=entry.duration_ms)
        console.print(f"  [bold white]Execution Time:[/bold white]    {dur_str}")

    if entry.row_count is not None:
        console.print(f"  [bold white]Rows Returned:[/bold white]     {entry.row_count}")

    if entry.error:
        console.print(f"  [bold red]Error Message:[/bold red]     {entry.error}")

    if entry.explanation:
        console.print(f"  [bold white]Explanation:[/bold white]       {entry.explanation}")

    console.print()
    console.print("[bold white]SQL Draft:[/bold white]")
    syntax = Syntax(entry.sql, lexer="sql", theme="monokai", line_numbers=False, word_wrap=True)
    console.print(syntax)
    console.print("[dim]------------------------------------------------------------[/dim]")
    if entry.entry_type != "multi_table_inspection":
        console.print(f"[dim]To restore this query into your current session, type: /history use {index}[/dim]\n")
    else:
        console.print("[dim]This entry was a multi-table inspection. To re-run, ask the question again at the prompt.[/dim]\n")


def _handle_history_use(
    index: int,
    state: WorkspaceSessionState,
    store: QueryHistoryStore,
    user_input_func: Callable[[str], str],
    db_service: Optional[object] = None,
    llm_service: Optional[object] = None,
) -> None:
    """Restore a historical draft into the active workspace session with mandatory checks and revalidation."""
    entry = store.get_entry(index)
    if not entry:
        console.print(f"[bold red]Error:[/bold red] History entry #{index} not found.")
        return

    if entry.entry_type == "multi_table_inspection":
        console.print(
            f"[bold yellow]Notice:[/bold yellow] History entry #{index} is a multi-table inspection, not a single-statement draft."
        )
        console.print(f"  [dim]Original request: '{entry.question}'[/dim]")
        console.print("  [dim]To re-run this inspection, enter the question directly at the prompt.[/dim]\n")
        return

    # Check 1: Active database must be selected
    if not state.active_database or not state.database_profile:
        console.print("[bold yellow]Cannot restore query:[/bold yellow] No active database selected.")
        console.print("[dim]Use /db to select a database profile matching the query's target dialect.[/dim]\n")
        return

    # Check 2: Active database and dialect must match
    active_dialect = state.database_profile.dialect.value
    if entry.dialect != active_dialect:
        console.print(
            f"[bold red]Dialect mismatch:[/bold red] Historical query is for [bold white]{entry.dialect}[/bold white], "
            f"but active database '{state.active_database}' uses [bold white]{active_dialect}[/bold white]."
        )
        console.print("[dim]Switch to a compatible database before restoring this query.[/dim]\n")
        return

    # Check 3: Schema verification and structural revalidation
    console.print(f"\n[cyan]Restoring and revalidating query draft from history #{index}...[/cyan]")
    reval = validate_sql_query(
        entry.sql,
        dialect=state.database_profile.dialect,
        schema=state.cached_schema,
    )

    restored_draft = QueryDraft(
        question=entry.question,
        sql=entry.sql,
        explanation=entry.explanation or f"Restored from history #{index}.",
        dialect=active_dialect,
        validation_status=reval.status_message,
        is_validated=reval.is_valid,
        is_parsed=reval.is_valid,
        schema_status=reval.schema_status,
        is_schema_verified=reval.is_schema_verified,
        tables_used=reval.tables_referenced,
        unknown_tables=reval.unknown_tables,
        unknown_columns=reval.unknown_columns,
        execution_status="Restored (pending execution)",
    )

    state.last_query_draft = restored_draft

    # Display draft preview
    display_query_draft(
        restored_draft,
        db_name=state.active_database,
        dialect_name=active_dialect,
    )

    if not restored_draft.is_validated or restored_draft.is_schema_verified is False or restored_draft.unknown_tables:
        console.print("[yellow]Restored query failed current schema validation. Draft retained for review.[/yellow]\n")
        return

    # Check 4: Require fresh explicit user approval (Never automatically execute)
    from support.workspace.execution_commands import handle_conversational_execution

    handle_conversational_execution(
        restored_draft,
        state,
        db_service=db_service,
        llm_service=llm_service,
        user_input_func=user_input_func,
    )


def _handle_history_clear(store: QueryHistoryStore, user_input_func: Callable[[str], str]) -> None:
    """Clear query history with explicit user confirmation."""
    try:
        confirm = user_input_func("Clear all stored query history? [y/N]: ").strip().lower()
    except (KeyboardInterrupt, EOFError):
        console.print("\n[dim]Clear cancelled.[/dim]\n")
        return

    if confirm in ("y", "yes"):
        store.clear()
        console.print("[bold green]✓[/bold green] Stored query history cleared.\n")
    else:
        console.print("[dim]Query history retained.[/dim]\n")
