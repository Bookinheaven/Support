"""General workspace commands (/help, /clear, /status, /schema)."""

from typing import Optional
from rich import box
from rich.table import Table

from support.cli.display import console
from support.workspace.display import (
    display_status_overview,
    display_workspace_header,
    display_workspace_help,
    display_workspace_title,
)
from support.workspace.state import WorkspaceSessionState


def handle_help(state: WorkspaceSessionState) -> None:
    """Handle /help command."""
    display_workspace_help()


def handle_clear(state: WorkspaceSessionState) -> None:
    """Handle /clear command: clear screen and reprint workspace title banner."""
    console.clear()
    display_workspace_title()


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
    table = Table(
        box=box.SIMPLE,
        header_style="bold cyan",
        show_header=True,
        pad_edge=False,
    )
    table.add_column("Table", style="bold white", no_wrap=True)
    table.add_column("Columns", justify="right", style="cyan", no_wrap=True)
    table.add_column("Foreign Keys", style="white")

    for t_name in sorted(schema.tables.keys()):
        tbl = schema.tables[t_name]
        fk_str = f"{len(tbl.foreign_keys)} FK(s)" if tbl.foreign_keys else "-"
        table.add_row(tbl.name, str(len(tbl.columns)), fk_str)

    console.print(table)
    console.print("[dim]Use [bold white]/schema refresh[/bold white] to re-inspect remote database schema.[/dim]\n")


def handle_details(state: WorkspaceSessionState) -> None:
    """Handle /details command: display detailed diagnostic report for current query draft."""
    if not state.last_query_draft:
        console.print("[yellow]No active query draft to inspect.[/yellow]")
        console.print("[dim]Ask a natural-language question first to generate a query draft.[/dim]\n")
        return

    from support.query.display import display_query_details

    display_query_details(state.last_query_draft, db_name=state.active_database)


def handle_diagram(
    state: WorkspaceSessionState,
    db_service: Optional[object] = None,
    arg: str = "",
    tables: Optional[list[str]] = None,
) -> None:
    """Handle diagram generation for active database workspace session."""
    if not state.active_database or not state.database_profile:
        console.print("[yellow]No active database selected. Use /db to choose an active database profile.[/yellow]\n")
        return

    profile = state.database_profile

    # MongoDB check
    from support.core.dialect import Dialect

    if profile.dialect == Dialect.MONGODB:
        console.print()
        console.print(
            f"[bold yellow]Notice:[/bold yellow] MongoDB collections in '{profile.name}' do not enforce relational foreign keys."
        )
        console.print("  [dim]Relational ER diagrams are designed for relational databases (PostgreSQL, MySQL, SQLite, SQL Server).[/dim]")
        console.print(f"  [dim]Use [bold white]/schema[/bold white] to view document collections and structures.[/dim]\n")
        return

    from support.query.schema_context import inspect_database_schema
    from support.schema.diagram import generate_mermaid_er, save_diagram_file

    clean_arg = arg.strip()
    should_refresh = False
    requested_tables = list(tables) if tables else []

    if clean_arg:
        parts = [p.strip() for p in clean_arg.replace(",", " ").split() if p.strip()]
        for p in parts:
            if p.lower() in ("refresh", "-r", "--refresh"):
                should_refresh = True
            elif not tables:
                requested_tables.append(p)

    if should_refresh or state.cached_schema is None:
        state.cached_schema = None
        with console.status(f"[cyan]Discovering schema for '{state.active_database}'...[/cyan]", spinner="dots"):
            try:
                state.cached_schema = inspect_database_schema(profile, db_service=db_service)
            except Exception as e:
                console.print(f"[bold red]Schema discovery failed:[/bold red] {e}\n")
                return

    schema = state.cached_schema
    if not schema or not schema.tables:
        console.print(f"[yellow]No accessible tables discovered in '{state.active_database}'.[/yellow]\n")
        return

    # Generate diagram
    try:
        mermaid_code = generate_mermaid_er(
            schema,
            table_filter=requested_tables or None,
            title=f"ER Diagram · {state.active_database} ({profile.dialect.value})",
        )

        mmd_path, html_path = save_diagram_file(
            mermaid_code,
            profile_name=state.active_database,
            overwrite=True,
            generate_html=True,
        )

        total_rendered_tables = len(requested_tables) if requested_tables else len(schema.tables)
        rel_count = mermaid_code.count("--")

        console.print()
        console.print(f"[bold green]✓[/bold green] Generated Mermaid ER diagram for [bold white]{state.active_database}[/bold white]")
        console.print(f"  [bold cyan]Mermaid file:[/bold cyan] {mmd_path}")
        if html_path:
            console.print(f"  [bold cyan]HTML viewer:[/bold cyan]  {html_path}")
        console.print(f"  [dim]Included: {total_rendered_tables} tables · {rel_count} relationships[/dim]")
        console.print("  [dim]Open HTML in any browser, preview .mmd in VS Code, or paste into https://mermaid.live[/dim]\n")

        from rich.syntax import Syntax
        syntax = Syntax(mermaid_code.strip(), lexer="mermaid", theme="monokai", line_numbers=False)
        console.print(syntax)
        console.print()

    except Exception as e:
        console.print(f"[bold red]Diagram generation failed:[/bold red] {e}\n")


