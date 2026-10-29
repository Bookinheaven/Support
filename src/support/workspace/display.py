"""Display rendering and Rich formatting for the Support interactive workspace."""

from rich import box
from rich.table import Table

from support.core.metadata import APP_DESCRIPTION, APP_NAME, APP_VERSION
from support.cli.display import console
from support.workspace.state import WorkspaceSessionState



def format_database_status_line(state: WorkspaceSessionState) -> str:
    """Format the active database status line for Rich display."""
    if state.active_database and state.database_profile:
        dialect_name = state.database_profile.dialect.value
        status_raw = state.database_status or "UNKNOWN"
        if "online" in status_raw.lower():
            status_color = "green"
        elif "checking" in status_raw.lower():
            status_color = "yellow"
        elif "untested" in status_raw.lower() or "configured" in status_raw.lower():
            status_color = "cyan"
        elif "timeout" in status_raw.lower():
            status_color = "yellow"
        else:
            status_color = "red"

        return (
            f"[bold white]{state.active_database}[/bold white] [dim]({dialect_name})[/dim] · "
            f"[{status_color}]{status_raw}[/{status_color}]"
        )
    elif state.active_database:
        status_raw = state.database_status or "UNKNOWN"
        return f"[bold white]{state.active_database}[/bold white] · [dim]{status_raw}[/dim]"
    else:
        return "[yellow]None selected[/yellow] [dim](Use /db to choose an active database)[/dim]"


def format_model_status_line(state: WorkspaceSessionState) -> str:
    """Format the configured model status line for Rich display."""
    prov = state.active_provider
    mod = state.active_model
    mod_status_raw = state.provider_status or "UNKNOWN"
    if "available" in mod_status_raw.lower():
        mod_color = "green"
    elif "checking" in mod_status_raw.lower():
        mod_color = "yellow"
    elif "untested" in mod_status_raw.lower() or "configured" in mod_status_raw.lower():
        mod_color = "cyan"
    elif "not installed" in mod_status_raw.lower() or "not configured" in mod_status_raw.lower():
        mod_color = "yellow"
    else:
        mod_color = "red"

    tested_suffix = ""
    if state.model_generation_tested:
        gen_stat = state.model_generation_status or "Passed"
        tested_suffix = f" · [magenta]Generation: {gen_stat}[/magenta]"

    return f"[bold white]{prov}/{mod}[/bold white] · [{mod_color}]{mod_status_raw}[/{mod_color}]{tested_suffix}"


def display_workspace_title() -> None:
    """Render workspace title and description banner."""
    console.print()
    console.print(
        f"[bold cyan]{APP_NAME}[/bold cyan] [bold white]Interactive Workspace[/bold white] [dim](v{APP_VERSION})[/dim]"
    )
    console.print(f"[dim]{APP_DESCRIPTION}[/dim]")
    console.print()


def display_workspace_header(state: WorkspaceSessionState) -> None:
    """Render a clean, restrained workspace banner adapting to terminal width."""
    display_workspace_title()

    # Database Status Line
    console.print(f"[bold cyan]Database:[/bold cyan] {format_database_status_line(state)}")

    # Model Status Line
    console.print(f"[bold cyan]Model:   [/bold cyan] {format_model_status_line(state)}")
    console.print()

    # Guidance and examples
    console.print("[dim]Type [bold white]/help[/bold white] for commands, [bold white]/exit[/bold white] to quit.[/dim]")
    console.print("[dim]Example queries:[/dim]")
    console.print("  [white]\"Show the top 5 customers by total order amount\"[/white]")
    console.print("  [white]\"List all orders placed in the last 30 days\"[/white]")
    console.print("  [white]\"What tables and relationships exist in this database?\"[/white]")
    console.print()


def display_status_overview(state: WorkspaceSessionState) -> None:
    """Render a comprehensive status overview for /status command."""
    from rich.panel import Panel

    grid = Table.grid(padding=(0, 2))
    grid.add_column(style="bold cyan", justify="right")
    grid.add_column(style="white")

    # Database section
    grid.add_row("Database:", format_database_status_line(state))
    if state.database_profile:
        grid.add_row("Endpoint:", state.database_profile.display_target)
    if state.db_details:
        grid.add_row("DB Detail:", f"[dim yellow]{state.db_details}[/dim yellow]")

    # Divider
    grid.add_row("", "")

    # Model section
    grid.add_row("Model:", format_model_status_line(state))
    if state.model_details:
        grid.add_row("Model Detail:", f"[dim yellow]{state.model_details}[/dim yellow]")
    if state.model_generation_tested:
        grid.add_row("Gen Test:", f"[magenta]{state.model_generation_status}[/magenta]")

    console.print()
    console.print(
        Panel(
            grid,
            title="[bold cyan]Support — Live Service Status[/bold cyan]",
            border_style="cyan",
            box=box.ROUNDED,
        )
    )
    console.print("[dim]Use [bold white]/status refresh[/bold white] to re-verify connectivity on demand.[/dim]\n")


def get_prompt_html(state: WorkspaceSessionState, first_turn: bool = True) -> str:
    """Generate prompt_toolkit HTML formatted prompt reflecting live state."""
    if not first_turn:
        return "&gt; "

    db_name = state.active_database or "None selected"
    db_target = f" ({state.database_profile.dialect.value})" if state.database_profile else ""
    db_status = state.database_status or "Unknown"

    if "online" in db_status.lower():
        db_color = "ansigreen"
    elif "checking" in db_status.lower():
        db_color = "ansiyellow"
    elif "untested" in db_status.lower() or "configured" in db_status.lower():
        db_color = "ansicyan"
    else:
        db_color = "ansired"

    model_prov = state.active_provider
    model_name = state.active_model
    model_status = state.provider_status or "Unknown"

    if "available" in model_status.lower():
        model_color = "ansigreen"
    elif "checking" in model_status.lower():
        model_color = "ansiyellow"
    elif "untested" in model_status.lower() or "configured" in model_status.lower():
        model_color = "ansicyan"
    elif "not installed" in model_status.lower() or "not configured" in model_status.lower():
        model_color = "ansiyellow"
    else:
        model_color = "ansired"

    return (
        f"<ansicyan>Database:</ansicyan> <b>{db_name}</b>{db_target} · <{db_color}>{db_status}</{db_color}>\n"
        f"<ansicyan>Model:   </ansicyan> <b>{model_prov}/{model_name}</b> · <{model_color}>{model_status}</{model_color}>\n\n"
        f"<ansibrightblack>Type a question or /help for commands.</ansibrightblack>\n"
        f"&gt; "
    )


def display_workspace_help() -> None:
    """Render workspace slash commands help table."""
    table = Table(
        box=box.SIMPLE,
        header_style="bold cyan",
        show_header=True,
        pad_edge=False,
    )
    table.add_column("Command", style="bold white", no_wrap=True)
    table.add_column("Description", style="white")

    table.add_row("/status", "Display or refresh active database and model connectivity status")
    table.add_row("/schema", "Inspect or refresh discovered schema for the active database (/schema [refresh])")
    table.add_row("/run", "Review safeguards and execute the latest approved read-only query draft")
    table.add_row("/db", "Inspect, test, add, or switch active database connection profile")
    table.add_row("/model", "Inspect and switch the active LLM provider and model for this session")
    table.add_row("/clear", "Clear visible terminal screen and redisplay workspace header")
    table.add_row("/exit, /quit", "Exit the interactive workspace cleanly")

    console.print()
    console.print("[bold cyan]Workspace Slash Commands[/bold cyan]")
    console.print(table)
    console.print("[dim]Enter natural-language questions directly at the prompt to generate draft SQL.[/dim]")
    console.print("[dim]Queries remain unexecuted drafts until explicitly approved and run via /run.[/dim]")
    console.print()



def display_natural_language_placeholder(
    query: str, state: WorkspaceSessionState
) -> None:
    """Inform user that natural-language SQL generation is planned for the next release."""
    console.print()
    console.print("[cyan]Query Intent Recorded[/cyan]")
    console.print(
        "  [dim]Natural-language query generation will be enabled in an upcoming release.[/dim]"
    )
    if state.active_database:
        console.print(f"  Target database: [bold white]{state.active_database}[/bold white]")
    else:
        console.print("  [yellow]Notice: No active database selected. Use /db to select one.[/yellow]")
    console.print(f"  Target model:    [bold white]{state.active_provider}/{state.active_model}[/bold white]")
    console.print()
