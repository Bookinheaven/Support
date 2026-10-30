"""Terminal rendering for structured mutation plans, previews, and transaction results."""

from rich.syntax import Syntax
from rich.table import Table
from rich import box

from support.cli.display import console
from support.core.formatting import format_duration
from support.db.models import ConnectionProfile
from support.mutation.models import ChangePlan, MutationResult, MutationType


def display_mutation_preview(
    plan: ChangePlan,
    profile: ConnectionProfile,
) -> None:
    """Render a structured change preview before requesting explicit approval."""
    console.print()

    # Title header
    op_title = {
        MutationType.INSERT: "Create Record",
        MutationType.UPDATE: "Modify Record(s)",
        MutationType.DELETE: "Delete Record(s)",
    }.get(plan.operation, plan.operation.value)

    header_color = "red" if plan.operation == MutationType.DELETE else "cyan"
    console.print(f"[bold {header_color}]Proposed change · {op_title}[/bold {header_color}]")
    console.print("[dim]------------------------------------------------------------[/dim]")
    console.print(f"  [bold white]Table:[/bold white]           {plan.table_name}")

    # 1. Fields to set / insert
    if plan.operation == MutationType.INSERT:
        console.print()
        console.print("  [bold white]Fields to insert:[/bold white]")
        for col, val in plan.fields_to_set.items():
            inferred_note = f" [dim]({plan.inferred_fields[col]})[/dim]" if col in plan.inferred_fields else ""
            console.print(f"    [cyan]{col}:[/cyan] {val}{inferred_note}")

        if plan.default_fields:
            console.print()
            console.print("  [bold white]Other fields:[/bold white]")
            for df in plan.default_fields:
                console.print(f"    [dim]• {df}[/dim]")

    # 2. Update preview with before / after diff
    elif plan.operation == MutationType.UPDATE:
        console.print()
        console.print("  [bold white]Proposed field changes:[/bold white]")
        for col, new_val in plan.fields_to_set.items():
            console.print(f"    [cyan]{col}:[/cyan] [green]{new_val}[/green]")

        if plan.preview_records:
            console.print()
            console.print(f"  [bold white]Affected records preview ({plan.matched_record_count} total):[/bold white]")
            for idx, rec in enumerate(plan.preview_records[:5], start=1):
                preview_items = [f"{k}={v}" for k, v in list(rec.items())[:4]]
                console.print(f"    [dim]Record #{idx}:[/dim] {', '.join(preview_items)}")

    # 3. Delete preview
    elif plan.operation == MutationType.DELETE:
        console.print()
        console.print(f"  [bold red]Target records to delete ({plan.matched_record_count} total):[/bold red]")
        if plan.preview_records:
            for idx, rec in enumerate(plan.preview_records[:5], start=1):
                preview_items = [f"{k}={v}" for k, v in list(rec.items())[:4]]
                console.print(f"    [dim]Record #{idx}:[/dim] {', '.join(preview_items)}")

        console.print()
        console.print("  [bold white]Selection condition:[/bold white]")
        cond_str = " AND ".join(f"{c.column} {c.operator} {c.value}" for c in plan.conditions)
        console.print(f"    [dim]{cond_str}[/dim]")

        if plan.foreign_key_warnings:
            console.print()
            console.print("  [bold yellow]Foreign Key Warnings:[/bold yellow]")
            for w in plan.foreign_key_warnings:
                console.print(f"    [yellow]⚠ {w}[/yellow]")

    # Summary safeguards
    console.print()
    console.print(f"  [bold white]Target database:[/bold white]  {profile.name} ({profile.dialect.value})")
    console.print(f"  [bold white]Operation:[/bold white]        {plan.operation.value}")
    console.print(f"  [bold white]Affected records:[/bold white] {plan.matched_record_count if plan.operation != MutationType.INSERT else 1}")
    console.print("  [bold white]Protection:[/bold white]      Parameterized statement · Transaction")
    console.print("[dim]------------------------------------------------------------[/dim]\n")


def display_mutation_result(result: MutationResult) -> None:
    """Render transactional outcome summary."""
    console.print()
    dur_str = format_duration(duration_ms=result.duration_ms)

    if result.is_success and result.committed:
        id_note = f" (id = {result.inserted_id})" if result.inserted_id is not None else ""
        row_label = "record" if result.affected_rows == 1 else "records"
        console.print(
            f"[bold green]✓[/bold green] Successfully executed [bold white]{result.operation}[/bold white] "
            f"on [bold white]{result.table}[/bold white]{id_note} · "
            f"[green]Committed ({result.affected_rows} {row_label})[/green] "
            f"[dim]({dur_str})[/dim]\n"
        )
    else:
        status_label = "Rolled Back" if result.rolled_back else "Failed"
        console.print(f"[bold red]✗ Operation {status_label}[/bold red]")
        console.print(f"  [red]{result.error_message}[/red]")
        console.print(f"  [dim]Database: {result.database} · Duration: {dur_str}[/dim]\n")
