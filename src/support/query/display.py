"""Terminal rendering for generated query drafts, validation results, and execution output."""

from datetime import date, datetime
from typing import TYPE_CHECKING, Any, Optional

from rich import box
from rich.panel import Panel
from rich.syntax import Syntax
from rich.table import Table

from support.cli.display import console
from support.core.formatting import format_duration, format_timeout, get_clean_safeguard_info
from support.query.models import QueryDraft
from support.query.privacy import redact_sensitive_result_rows

if TYPE_CHECKING:
    from support.query.executor import ExecutionConfig, QueryResult


def _get_validation_suggestion(reason: str, draft: QueryDraft) -> Optional[str]:
    """Provide a concise, actionable suggestion for common validation rejections."""
    lower_r = reason.lower()
    if "unconditional delete" in lower_r or "unconditional update" in lower_r:
        target = f" in '{draft.target_table}'" if draft.target_table else ""
        return f"Specify a filter or row limit{target} (e.g., 'delete 5 newest users' or 'update users where status = inactive')."
    if "direct limit" in lower_r:
        return "Use a key-bounded subquery filter like 'WHERE id IN (SELECT id FROM ... LIMIT N)' on this dialect."
    if "ambiguous" in lower_r or "clarification" in lower_r:
        return "Provide more specific criteria or column names."
    if "unknown table" in lower_r or getattr(draft, "unknown_tables", []):
        tbls = ", ".join(draft.unknown_tables) if draft.unknown_tables else "specified table"
        return f"Table '{tbls}' was not found. Use /schema to list accessible tables."
    return None


def display_query_draft(
    draft: QueryDraft,
    db_name: Optional[str] = None,
    dialect_name: Optional[str] = None,
    max_rows: int = 100,
    timeout_seconds: float = 10.0,
) -> None:
    """Render a concise conversational query draft preview with clear execution status."""
    console.print()

    # 0. Clarification request display
    if getattr(draft, "is_clarification", False):
        console.print(f"[bold cyan]{draft.clarification}[/bold cyan]\n")
        return

    # 1. Validation failure / rejected state
    if not draft.is_validated:
        reason = draft.validation_status
        if reason.startswith("Rejected: "):
            reason = reason[10:]
        console.print(f"[bold red]Query rejected · {reason}[/bold red]")
        if draft.target_table:
            console.print(f"  [dim]Target table: {draft.target_table}[/dim]")
        suggestion = _get_validation_suggestion(reason, draft)
        if suggestion:
            console.print(f"  [dim cyan]Suggestion: {suggestion}[/dim cyan]")
        if draft.sql:
            console.print()
            console.print("[bold white]Draft SQL:[/bold white]")
            syntax = Syntax(draft.sql.strip(), lexer="sql", theme="monokai", line_numbers=False, word_wrap=True)
            console.print(syntax)
        console.print()
        return

    # 2. Schema clarification needed
    if draft.is_schema_verified is False:
        console.print("[bold yellow]Schema clarification needed[/bold yellow]")
        if draft.schema_status and draft.schema_status != "Not checked":
            console.print(f"  [dim]{draft.schema_status}[/dim]")

    # 3. Short explanation when useful
    if draft.explanation and draft.explanation.strip():
        console.print(f"[dim]{draft.explanation.strip()}[/dim]")

    # 4. Single SQL preview
    if draft.sql:
        console.print("[bold white]SQL:[/bold white]")
        syntax = Syntax(
            draft.sql.strip(),
            lexer="sql",
            theme="monokai",
            line_numbers=False,
            word_wrap=True,
        )
        console.print(syntax)
        console.print()

    # 5. Correction note if query was repaired
    if draft.correction_notes:
        console.print(f"[dim cyan]Correction note: {draft.correction_notes}[/dim cyan]\n")

    # 6. Standalone mutation summary (only when display_query_draft is called directly outside approval prompt)
    if not draft.is_readonly:
        if getattr(draft, "is_unrestricted", False):
            console.print(f"[bold red]⚠️  Full-table mutation on '{draft.target_table or 'table'}'[/bold red]\n")
        elif getattr(draft, "is_bounded", False) and getattr(draft, "mutation_limit", None):
            console.print(f"[cyan]Bounded mutation · Maximum {draft.mutation_limit} records in '{draft.target_table or 'table'}'[/cyan]\n")
        else:
            console.print(f"[yellow]Filtered mutation · Target: '{draft.target_table or 'table'}'[/yellow]\n")


def display_query_details(
    draft: QueryDraft,
    db_name: Optional[str] = None,
    performance: Optional[Any] = None,
) -> None:
    """Render full diagnostic information for the current query draft on demand (/details)."""
    console.print()
    console.print("[bold cyan]Query Draft Diagnostics[/bold cyan]")
    console.print("[dim]------------------------------------------------------------[/dim]")

    table = Table(box=box.SIMPLE, show_header=False, padding=(0, 1))
    table.add_column("Property", style="bold white", width=22)
    table.add_column("Value")

    table.add_row("Question", draft.question or "[dim]N/A[/dim]")
    table.add_row("Database / Dialect", f"{db_name or 'N/A'} ({draft.dialect})")
    table.add_row("Operation", f"{draft.operation} ({'Read-only' if draft.is_readonly else 'Mutation'})")

    provider_model = f"{draft.provider} / {draft.model}" if draft.provider else "[dim]N/A[/dim]"
    table.add_row("LLM Provider / Model", provider_model)

    parse_val = "[green]Successful[/green]" if draft.is_parsed else "[red]Failed[/red]"
    table.add_row("Parsing", parse_val)

    val_style = "green" if draft.is_validated else "red"
    table.add_row("Validation Status", f"[{val_style}]{draft.validation_status}[/{val_style}]")

    if draft.is_schema_verified is True:
        schema_val = f"[green]Verified[/green] ({draft.schema_status})"
    elif draft.is_schema_verified is False:
        schema_val = f"[yellow]Needs clarification[/yellow] ({draft.schema_status})"
    else:
        schema_val = f"[dim]{draft.schema_status}[/dim]"
    table.add_row("Schema Verification", schema_val)

    if draft.schema_coverage:
        table.add_row("Schema Context", draft.schema_coverage)

    if draft.tables_used:
        table.add_row("Referenced Tables", ", ".join(draft.tables_used))
    if draft.unknown_tables:
        table.add_row("Unknown Tables", f"[red]{', '.join(draft.unknown_tables)}[/red]")
    if draft.unknown_columns:
        table.add_row("Unknown Columns", f"[red]{', '.join(draft.unknown_columns)}[/red]")

    # Mutation scope diagnostics
    if not draft.is_readonly:
        table.add_row("Mutation Scope", getattr(draft, "mutation_scope", "N/A"))
        table.add_row("Target Table", draft.target_table or "Unknown")
        limit_str = f"{draft.mutation_limit} records" if getattr(draft, "is_bounded", False) else "None (Uncapped)"
        table.add_row("Guaranteed Row Bound", limit_str)
        table.add_row(
            "Selection Determinism",
            getattr(draft, "determinism_reason", "N/A")
            or ("Deterministic" if getattr(draft, "is_deterministic", False) else "Nondeterministic"),
        )
        if getattr(draft, "selection_criteria", None):
            table.add_row("Selection Criteria", draft.selection_criteria)

    table.add_row("Execution Status", f"[dim]{draft.execution_status}[/dim]")

    if draft.repair_attempts > 0:
        table.add_row("Repair Attempts", str(draft.repair_attempts))
    if draft.explanation:
        table.add_row("Full Explanation", draft.explanation)
    if draft.correction_notes:
        table.add_row("Correction Notes", draft.correction_notes)

    console.print(table)

    if performance and hasattr(performance, "stage_durations"):
        console.print()
        console.print("[bold cyan]Performance Diagnostics[/bold cyan]")
        console.print("[dim]------------------------------------------------------------[/dim]")
        perf_table = Table(box=box.SIMPLE, show_header=False, padding=(0, 1))
        perf_table.add_column("Metric / Stage", style="bold white", width=26)
        perf_table.add_column("Value")

        perf_table.add_row("Total Response Time", format_duration(duration_ms=performance.total_duration_ms))
        perf_table.add_row("LLM Requests", str(performance.llm_calls_count))
        if performance.total_tokens is not None:
            perf_table.add_row(
                "Tokens (in / out / total)",
                f"{performance.total_prompt_tokens or '-'} / {performance.total_completion_tokens or '-'} / {performance.total_tokens}",
            )
        perf_table.add_row("Validation Failures", str(performance.validation_failures_count))
        perf_table.add_row("Repair Attempts", str(performance.repair_attempts_count))

        for stg, dur in performance.stage_durations.items():
            if stg != "total_duration":
                perf_table.add_row(f"  • {stg}", f"{format_duration(duration_ms=dur)} ({dur:.1f} ms)")

        console.print(perf_table)

    console.print("[dim]------------------------------------------------------------[/dim]\n")


def display_mutation_preview(
    draft: QueryDraft,
    profile_name: str,
    dialect_name: str,
    affected_row_estimate: Optional[int] = None,
) -> None:
    """Render a compact mutation preview before approval with clear limits, selection, and warnings."""
    console.print()
    is_unres = getattr(draft, "is_unrestricted", False)

    # 1. Title / Header
    if is_unres:
        console.print(f"[bold red]⚠️  WARNING: FULL-TABLE {draft.operation} OPERATION[/bold red]")
        console.print(
            f"[bold red]This operation will modify or delete ALL records in '{draft.target_table or 'table'}' without row limit or filter.[/bold red]\n"
        )
    elif draft.question and draft.question.strip():
        q_text = draft.question.strip()
        if not q_text.endswith("?"):
            q_text = q_text[0].upper() + q_text[1:] + "?"
        console.print(f"[bold white]{q_text}[/bold white]\n")
    else:
        op_title = draft.operation.title() if draft.operation else "Mutation"
        console.print(f"[bold white]{op_title} records in '{draft.target_table or 'table'}'?[/bold white]\n")

    # 2. Database and Target
    active_db = profile_name or "active database"
    dialect_str = f" ({dialect_name})" if dialect_name and dialect_name != active_db else ""
    console.print(f"  [bold white]Database:[/bold white]  {active_db}{dialect_str}")
    console.print(f"  [bold white]Target:[/bold white]    {draft.target_table or 'Unknown'}")

    # 3. Selection & Row Bounds
    if is_unres:
        console.print("  [bold white]Selection:[/bold white] [bold red]None (Entire table targeted)[/bold red]")
        if affected_row_estimate is not None:
            console.print(f"  [bold white]Estimated affected:[/bold white] ~{affected_row_estimate} rows [dim](entire table)[/dim]")
        console.print("  [bold white]Guaranteed maximum:[/bold white] [bold red]None (Unrestricted - all table rows)[/bold red]")
    elif getattr(draft, "is_bounded", False) and getattr(draft, "mutation_limit", None) is not None:
        limit_cnt = draft.mutation_limit
        if getattr(draft, "is_ordered", False):
            if getattr(draft, "is_deterministic", False):
                sql_upper = draft.sql.upper()
                if "DESC" in sql_upper and any(k in draft.sql.lower() for k in ("created", "date", "time", "id")):
                    order_label = "Newest first"
                elif "ASC" in sql_upper and any(k in draft.sql.lower() for k in ("created", "date", "time", "id")):
                    order_label = "Oldest first"
                else:
                    order_label = "Ordered with unique tie-breaker"
            else:
                order_label = "Ordered (ties may be arbitrary)"
        else:
            order_label = "Arbitrary selection (no ORDER BY)"

        console.print(f"  [bold white]Selection:[/bold white] {order_label} · [cyan]Maximum {limit_cnt} records[/cyan]")
        if affected_row_estimate is not None and affected_row_estimate < limit_cnt:
            console.print(f"  [bold white]Estimated affected:[/bold white] ~{affected_row_estimate} rows [dim](capped by maximum of {limit_cnt})[/dim]")
    else:
        crit = draft.selection_criteria or "Filtered by WHERE criteria"
        console.print(f"  [bold white]Selection:[/bold white] {crit} · [yellow]All matching rows may be modified[/yellow]")
        if affected_row_estimate is not None:
            console.print(f"  [bold white]Estimated affected:[/bold white] ~{affected_row_estimate} rows [dim](pre-execution estimate, not guaranteed)[/dim]")
        console.print("  [bold white]Guaranteed maximum:[/bold white] [yellow]None (uncapped by row limit)[/yellow]")

    # 4. Single SQL block
    console.print()
    console.print("[bold white]SQL:[/bold white]")
    syntax = Syntax(draft.sql.strip(), lexer="sql", theme="monokai", line_numbers=False, word_wrap=True)
    console.print(syntax)
    console.print()


def display_execution_preview(
    draft: QueryDraft,
    profile_name: str,
    dialect_name: str,
    config: "ExecutionConfig",
    affected_row_estimate: Optional[int] = None,
) -> None:
    """Render preview panel before execution approval."""
    if not draft.is_readonly:
        display_mutation_preview(
            draft=draft,
            profile_name=profile_name,
            dialect_name=dialect_name,
            affected_row_estimate=affected_row_estimate,
        )
        return

    console.print()
    active_db = profile_name or "active database"
    dialect_str = f" ({dialect_name})" if dialect_name and dialect_name != active_db else ""
    console.print(f"[bold white]Execute read-only query on {active_db}{dialect_str}?[/bold white]\n")
    console.print("[bold white]SQL:[/bold white]")
    syntax = Syntax(draft.sql.strip(), lexer="sql", theme="monokai", line_numbers=False, word_wrap=True)
    console.print(syntax)
    console.print()



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
    view_mode: str = "auto",
) -> None:
    """Render query execution results with defense-in-depth redaction and adaptive layout selection.

    Args:
        result: Query execution output.
        view_mode: Presentation mode ('auto' chooses layout automatically, 'table' for compact table,
                   'vertical' for record cards, 'wide' for all columns).
    """
    console.print()
    formatted_duration = format_duration(duration_ms=result.duration_ms)
    from support.core.performance import get_current_tracker
    import time
    tracker = get_current_tracker()
    t_fmt0 = time.perf_counter()

    if not result.is_success:
        console.print("[bold red]Execution Failed[/bold red]")
        console.print(f"[red]{result.error_message}[/red]")
        console.print(f"[dim]Failed in {formatted_duration} · Database: {result.database} ({result.dialect})[/dim]\n")
        return

    if result.row_count == 0:
        console.print("[yellow]Query executed successfully, but returned 0 rows.[/yellow]")
        console.print(f"[dim]Completed in {formatted_duration} · Database: {result.database} ({result.dialect})[/dim]\n")
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

    # Adaptive layout decision when in 'auto' mode
    if mode == "auto":
        term_width = console.width or 80
        col_widths = [len(str(c)) for c in result.columns]
        for row in safe_rows[:10]:
            for i, val in enumerate(row):
                if i < len(col_widths):
                    val_str = str(val)[:40] if val is not None else "NULL"
                    col_widths[i] = max(col_widths[i], len(val_str))

        estimated_table_width = sum(col_widths) + len(col_widths) * 3 + 4
        if total_cols > 5 or estimated_table_width > (term_width - 5):
            mode = "vertical"
        else:
            mode = "table"

    # 1. Vertical / Record View (best for wide results or >5 columns)
    if mode in ("vertical", "v", "record"):
        console.print(f"[bold cyan]Query Results[/bold cyan] [dim](Record View · {result.row_count} rows, {total_cols} columns)[/dim]")
        max_col_w = max(len(str(c)) for c in result.columns) if result.columns else 10
        for idx, row in enumerate(safe_rows, start=1):
            console.print(f"[dim]── Record {idx} of {result.row_count} ──────────────────────────────────────[/dim]")
            for col_name, val in zip(result.columns, row):
                formatted = _format_cell_value(val, max_len=140)
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

    clean_protection, clean_timeout = get_clean_safeguard_info(result.safeguard_applied)
    console.print(
        f"[dim]Completed in {formatted_duration} · Protection: {clean_protection} · {clean_timeout}[/dim]\n"
    )
    if tracker:
        tracker.stage_durations["result_formatting"] = (
            tracker.stage_durations.get("result_formatting", 0.0) + (time.perf_counter() - t_fmt0) * 1000.0
        )
