"""General workspace commands (/help, /clear, /status, /schema)."""

from typing import Optional

from support.cli.display import console
from support.workspace.display import (
    display_status_overview,
    display_workspace_header,
    display_workspace_help,
)
from support.workspace.state import WorkspaceSessionState


def handle_help(state: WorkspaceSessionState) -> None:
    """Handle /help command."""
    display_workspace_help()


def handle_clear(state: WorkspaceSessionState) -> None:
    """Handle /clear command: clear screen and reprint workspace header."""
    console.clear()
    display_workspace_header(state)


def handle_status(
    state: WorkspaceSessionState,
    checker: Optional[object] = None,
    arg: str = "",
) -> None:
    """Handle /status command: display overview or refresh active checks."""
    clean_arg = arg.strip().lower()
    if clean_arg in ("refresh", "-r", "--refresh"):
        state.cached_schema = None
        if checker and hasattr(checker, "refresh"):
            with console.status("[cyan]Verifying database and model connectivity...[/cyan]", spinner="dots"):
                checker.refresh()
                if hasattr(checker, "wait_completion"):
                    checker.wait_completion(timeout=5.0)
            console.print("[bold green]✓[/bold green] Refreshed active database and model connectivity checks.")
        display_status_overview(state)
    else:
        display_status_overview(state)


def handle_schema(
    state: WorkspaceSessionState,
    db_service: Optional[object] = None,
    arg: str = "",
) -> None:
    """Handle /schema command: inspect or refresh discovered schema for active database."""
    clean_arg = arg.strip().lower()

    if not state.active_database or not state.database_profile:
        console.print("[yellow]No active database selected. Use /db to choose an active database.[/yellow]")
        return

    from support.query.schema_context import inspect_database_schema

    if clean_arg in ("refresh", "-r", "--refresh"):
        state.cached_schema = None
        with console.status(f"[cyan]Refreshing schema for '{state.active_database}'...[/cyan]", spinner="dots"):
            try:
                state.cached_schema = inspect_database_schema(state.database_profile, db_service=db_service)
            except Exception as e:
                console.print(f"[bold red]Schema inspection failed:[/bold red] {e}")
                return

        table_count = len(state.cached_schema.tables) if state.cached_schema else 0
        console.print(
            f"[bold green]✓[/bold green] Refreshed schema for [bold white]{state.active_database}[/bold white] "
            f"[dim]({table_count} tables discovered)[/dim]."
        )
        return

    # Normal inspection
    if state.cached_schema is None:
        with console.status(f"[cyan]Inspecting schema for '{state.active_database}'...[/cyan]", spinner="dots"):
            try:
                state.cached_schema = inspect_database_schema(state.database_profile, db_service=db_service)
            except Exception as e:
                console.print(f"[bold red]Schema inspection failed:[/bold red] {e}")
                return

    schema = state.cached_schema
    if not schema or not schema.tables:
        console.print(f"[yellow]No accessible tables discovered in '{state.active_database}'.[/yellow]")
        return

    console.print()
    console.print(
        f"[bold cyan]Discovered Schema:[/bold cyan] [bold white]{state.active_database}[/bold white] "
        f"[dim]({schema.dialect.value}, {len(schema.tables)} tables, {schema.total_columns} columns, {schema.total_foreign_keys} FKs)[/dim]"
    )
    for t_name in sorted(schema.tables.keys()):
        tbl = schema.tables[t_name]
        fk_note = f", {len(tbl.foreign_keys)} FKs" if tbl.foreign_keys else ""
        console.print(f"  [cyan]•[/cyan] [bold white]{tbl.name}[/bold white] [dim]({len(tbl.columns)} cols{fk_note})[/dim]")

    console.print("\n[dim]Use [bold white]/schema refresh[/bold white] to re-inspect remote database schema.[/dim]\n")
