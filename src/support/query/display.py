"""Terminal rendering for generated query drafts, validation results, and execution output."""

from datetime import date, datetime
from typing import TYPE_CHECKING, Any, Optional

from rich import box
from rich.panel import Panel
from rich.syntax import Syntax
from rich.table import Table

from support.cli.display import console
from support.query.models import QueryDraft
from support.query.privacy import redact_sensitive_result_rows

if TYPE_CHECKING:
    from support.query.executor import ExecutionConfig, QueryResult


def display_query_draft(draft: QueryDraft) -> None:
    """Render a generated SQL query draft with explanation, validation, schema alignment, and execution status."""
    console.print()

    if draft.sql:
        console.print("[bold white]Draft SQL[/bold white]")
        console.print("[dim]--------[/dim]")
        syntax = Syntax(
            draft.sql,
            lexer="sql",
            theme="monokai",
            line_numbers=False,
            word_wrap=True,
        )
        console.print(syntax)
        console.print()

    if draft.explanation:
        console.print("[bold white]Explanation:[/bold white]")
        console.print(f"[white]{draft.explanation}[/white]")
        console.print()

    if draft.schema_coverage and "subset" in draft.schema_coverage.lower():
        console.print(f"[dim]Schema Context: {draft.schema_coverage}[/dim]")

    # 1. Parsing status
    if draft.sql and draft.is_parsed:
        console.print("[bold white]Parsing:   [/bold white] [green]Successful[/green]")
    elif draft.sql and not draft.is_parsed:
        console.print("[bold white]Parsing:   [/bold white] [red]Failed[/red]")

    # 2. Structural/read-only validation
    if draft.is_validated:
        console.print("[bold white]Validation:[/bold white] [bold green]Passed[/bold green]")
    else:
        reason = draft.validation_status
        if not reason.startswith("Rejected"):
            reason = f"Rejected: {reason}"
        console.print(f"[bold white]Validation:[/bold white] [bold red]{reason}[/bold red]")

    # 3. Schema alignment
    if draft.is_schema_verified is True:
        console.print(f"[bold white]Schema:    [/bold white] [green]{draft.schema_status}[/green]")
    elif draft.is_schema_verified is False:
        console.print(f"[bold white]Schema:    [/bold white] [yellow]{draft.schema_status}[/yellow]")
    elif draft.schema_status != "Not checked":
        console.print(f"[bold white]Schema:    [/bold white] [dim]{draft.schema_status}[/dim]")

    # 4. Execution status
    console.print(f"[bold white]Execution: [/bold white] [dim]{draft.execution_status}[/dim]")

    if draft.is_validated and draft.is_schema_verified is True:
        console.print("[dim]Use [bold white]/run[/bold white] to review safeguards and execute this query.[/dim]")

    console.print()


def display_execution_preview(
    draft: QueryDraft,
    profile_name: str,
    dialect_name: str,
    config: "ExecutionConfig",
) -> None:
    """Render preview panel with safeguards, limits, and SQL to be approved before execution."""
    console.print()
    console.print(f"[bold cyan]Execute Read-Only Query Draft[/bold cyan] · [dim]{profile_name} ({dialect_name})[/dim]")
    console.print("[dim]------------------------------------------------------------[/dim]")
    console.print(f"  [bold white]Database:[/bold white]   {profile_name} ({dialect_name})")
    console.print(f"  [bold white]Validation:[/bold white] [green]{draft.validation_status}[/green]")
    console.print(f"  [bold white]Schema:[/bold white]     [green]{draft.schema_status}[/green]")
    console.print(f"  [bold white]Limits:[/bold white]     Max {config.max_rows} rows · Timeout {config.timeout_seconds:.1f}s")
    console.print()
    console.print("[bold white]SQL to be executed:[/bold white]")
    syntax = Syntax(draft.sql, lexer="sql", theme="monokai", line_numbers=False, word_wrap=True)
    console.print(syntax)
    console.print("[dim]------------------------------------------------------------[/dim]")


def _format_cell_value(val: Any, max_len: int = 40) -> str:
    """Format an individual cell value with type-aware styling and bounded width."""
    if val is None:
        return "[dim]NULL[/dim]"
    elif isinstance(val, bool):
        return f"[magenta]{val}[/magenta]"
    elif isinstance(val, (datetime, date)):
        return f"[blue]{val.isoformat()}[/blue]"
    elif isinstance(val, (bytes, bytearray)):
        return f"[dim]<bytes: {len(val)}B>[/dim]"
    elif val == "[REDACTED]":
        return "[red][REDACTED][/red]"
    else:
        str_val = str(val).replace("\n", " ").replace("\r", "")
        if len(str_val) > max_len:
            str_val = str_val[: max_len - 3] + "..."
        return str_val


def display_query_result(
    result: "QueryResult",
    view_mode: str = "table",
) -> None:
    """Render query execution results with defense-in-depth redaction and wide-result views.

    Args:
        result: Query execution output.
        view_mode: Presentation mode ('table' for compact table, 'vertical' for record cards, 'wide' for all columns).
    """
    console.print()
    if not result.is_success:
        console.print("[bold red]Execution Failed[/bold red]")
        console.print(f"[red]{result.error_message}[/red]")
        console.print(f"[dim]Duration: {result.duration_ms:.1f}ms · Database: {result.database} ({result.dialect})[/dim]\n")
        return

    if result.row_count == 0:
        console.print("[yellow]Query executed successfully, but returned 0 rows.[/yellow]")
        console.print(f"[dim]Duration: {result.duration_ms:.1f}ms · Database: {result.database} ({result.dialect})[/dim]\n")
        return

    # Defense-in-depth: apply result-level sensitive field redaction
    safe_rows, redacted_cols = redact_sensitive_result_rows(
        columns=result.columns,
        rows=result.rows,
        sql=result.executed_sql,
        dialect=result.dialect,
    )

    mode = view_mode.strip().lower()
    total_cols = len(result.columns)

    # 1. Vertical / Record View
    if mode in ("vertical", "v", "record"):
        console.print(f"[bold cyan]Query Results[/bold cyan] [dim](Vertical Record View · {result.row_count} rows, {total_cols} columns)[/dim]")
        for idx, row in enumerate(safe_rows, start=1):
            console.print(f"[dim]── Record {idx} of {result.row_count} ──────────────────────────────────────[/dim]")
            max_col_w = max(len(str(c)) for c in result.columns) if result.columns else 10
            for col_name, val in zip(result.columns, row):
                formatted = _format_cell_value(val, max_len=120)
                console.print(f"  [bold cyan]{col_name:<{max_col_w}}[/bold cyan] : {formatted}")
        console.print()

    # 2. Table or Wide View
    else:
        max_display_cols = 6 if mode != "wide" and total_cols > 6 else total_cols
        displayed_columns = result.columns[:max_display_cols]
        omitted_columns = result.columns[max_display_cols:]

        table = Table(
            box=box.ROUNDED,
            header_style="bold cyan",
            show_lines=False,
            show_edge=True,
            expand=False,
        )

        for col in displayed_columns:
            table.add_column(str(col), overflow="ellipsis")

        for row in safe_rows:
            formatted_row = [_format_cell_value(val, max_len=40) for val in row[:max_display_cols]]
            table.add_row(*formatted_row)

        console.print(table)

        if omitted_columns:
            omitted_str = ", ".join(omitted_columns)
            console.print(
                f"[dim]Note: Displaying {len(displayed_columns)} of {total_cols} columns. "
                f"Omitted: {omitted_str}[/dim]"
            )
            console.print("[dim]Use [bold white]/run vertical[/bold white] (or [bold white]/run wide[/bold white]) to inspect all columns.[/dim]")

    if redacted_cols:
        console.print(f"[dim]Notice: Sensitive credential field(s) were redacted: {', '.join(redacted_cols)}[/dim]")

    if result.is_truncated:
        console.print(
            f"[bold yellow]Showing first {result.row_count} rows (truncated).[/bold yellow] "
            f"[dim]Max row limit reached; additional rows exist in database.[/dim]"
        )
    else:
        console.print(f"[green]{result.row_count} row(s) returned.[/green]")

    safeguard = result.safeguard_applied or "Active"
    console.print(f"[dim]Duration: {result.duration_ms:.1f}ms · Safeguard: {safeguard}[/dim]\n")
