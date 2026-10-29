"""Rich CLI display formatting and tables rendering."""

from pathlib import Path
from typing import Optional
from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.text import Text
from rich import box

from support.core.dialect import Dialect
from support.schema.base import SchemaDiscoveryResult
from support.schema.models import DatabaseSchema, TableSchema


console = Console()


def display_discovery_header(
    target_path: Path,
    dialect: Dialect,
    result: SchemaDiscoveryResult,
) -> None:
    """Render executive overview panel."""
    grid = Table.grid(padding=(0, 2))
    grid.add_column(style="bold cyan", justify="right")
    grid.add_column(style="white")

    grid.add_row("Target:", str(target_path.resolve()))
    grid.add_row("Dialect:", f"{dialect.display_name} ({dialect.value})")
    grid.add_row(
        "Files Discovered:",
        f"{len(result.discovered_files)} found ({len(result.parsed_files)} successfully parsed)",
    )
    if result.failed_files:
        grid.add_row("Files Failed:", f"[bold red]{len(result.failed_files)}[/bold red]")
    if result.warnings:
        grid.add_row("Warnings / Conflicts:", f"[yellow]{len(result.warnings)}[/yellow]")

    console.print(
        Panel(
            grid,
            title="[bold blue]Support — Database Schema Discovery[/bold blue]",
            border_style="blue",
            box=box.ROUNDED,
        )
    )


def display_schema_summary(schema: DatabaseSchema) -> None:
    """Render quick metrics summary."""
    summary_table = Table(box=box.SIMPLE_HEAVY, show_header=True, header_style="bold magenta")
    summary_table.add_column("Tables", justify="center")
    summary_table.add_column("Columns", justify="center")
    summary_table.add_column("Foreign Keys", justify="center")

    summary_table.add_row(
        str(len(schema.tables)),
        str(schema.total_columns),
        str(schema.total_foreign_keys),
    )
    console.print(summary_table)


def display_tables(schema: DatabaseSchema) -> None:
    """Render each table and its columns, constraints, and relationships."""
    if not schema.tables:
        console.print(
            Panel(
                "[yellow]No database tables were discovered.[/yellow]\n"
                "Check that the directory contains recognized SQL DDL or SQLite files.",
                title="Discovery Result",
                border_style="yellow",
            )
        )
        return

    for table_name in schema.table_names:
        table: TableSchema = schema.tables[table_name]

        # Table header info
        table_title = f"[bold green]Table: {table.full_name}[/bold green]"
        if table.source_origins:
            origins = ", ".join(Path(o).name for o in table.source_origins)
            table_title += f" [dim]({origins})[/dim]"

        col_table = Table(
            title=table_title,
            box=box.ROUNDED,
            header_style="bold cyan",
            expand=True,
        )
        col_table.add_column("Column", style="bold white", ratio=2)
        col_table.add_column("Type", style="cyan", ratio=2)
        col_table.add_column("Nullable", justify="center", ratio=1)
        col_table.add_column("Default", style="dim", ratio=2)
        col_table.add_column("Key / Constraints", style="yellow", ratio=3)

        for col in table.columns:
            constraints: list[str] = []
            if col.is_primary_key or col.name in table.primary_key:
                constraints.append("[bold red]PK[/bold red]")
            if col.is_unique:
                constraints.append("[bold magenta]UQ[/bold magenta]")

            # Check if column is constrained by a foreign key
            for fk in table.foreign_keys:
                if col.name in fk.constrained_columns:
                    target = f"{fk.referred_table}.{', '.join(fk.referred_columns)}"
                    constraints.append(f"[blue]FK -> {target}[/blue]")

            null_str = "YES" if col.nullable is True else ("NO" if col.nullable is False else "-")
            null_style = "green" if col.nullable is True else ("red" if col.nullable is False else "dim")

            dflt_str = col.default_value if col.default_value is not None else "-"

            col_table.add_row(
                col.name,
                col.data_type,
                Text(null_str, style=null_style),
                dflt_str,
                " ".join(constraints) if constraints else "-",
            )

        console.print(col_table)

        # Print foreign keys if composite or multiple
        if table.foreign_keys:
            fk_lines = []
            for fk in table.foreign_keys:
                name_str = f"[bold]{fk.name}: [/bold]" if fk.name else ""
                fk_lines.append(f"  * {name_str}{fk.display_str}")
            console.print(
                Panel(
                    "\n".join(fk_lines),
                    title=f"[dim]Foreign Keys for {table.name}[/dim]",
                    border_style="dim blue",
                    box=box.SQUARE,
                )
            )

        # Print unique constraints if composite
        composite_uqs = [uq for uq in table.unique_constraints if len(uq.columns) > 1]
        if composite_uqs:
            uq_lines = []
            for uq in composite_uqs:
                name_str = f"[bold]{uq.name}: [/bold]" if uq.name else ""
                uq_lines.append(f"  * {name_str}UNIQUE ({', '.join(uq.columns)})")
            console.print(
                Panel(
                    "\n".join(uq_lines),
                    title=f"[dim]Unique Constraints for {table.name}[/dim]",
                    border_style="dim magenta",
                    box=box.SQUARE,
                )
            )

        console.print()


def display_diagnostics(result: SchemaDiscoveryResult) -> None:
    """Render discovery diagnostics, warnings, and file failures."""
    if result.warnings:
        warn_table = Table(
            title="[bold yellow]Discovery Warnings & Conflicts[/bold yellow]",
            box=box.ROUNDED,
            header_style="bold yellow",
        )
        warn_table.add_column("Severity", justify="center", width=12)
        warn_table.add_column("Origin", style="dim", width=25)
        warn_table.add_column("Message")

        for w in result.warnings:
            sev_color = "red" if w.severity == "conflict" else "yellow"
            origin_name = Path(w.file_path).name if w.file_path else "Project"
            warn_table.add_row(f"[{sev_color}]{w.severity.upper()}[/{sev_color}]", origin_name, w.message)

        console.print(warn_table)
        console.print()

    if result.failed_files:
        err_table = Table(
            title="[bold red]Unsupported / Failed Files[/bold red]",
            box=box.ROUNDED,
            header_style="bold red",
        )
        err_table.add_column("File", style="bold white", width=35)
        err_table.add_column("Failure Reason", style="red")

        for file_path, reason in result.failed_files.items():
            err_table.add_row(Path(file_path).name, reason)

        console.print(err_table)
        console.print()


def display_error(title: str, message: str, details: Optional[str] = None) -> None:
    """Display user-facing formatted error panel."""
    content = f"[bold red]{message}[/bold red]"
    if details:
        content += f"\n[dim]{details}[/dim]"
    console.print(Panel(content, title=f"[bold red]{title}[/bold red]", border_style="red", box=box.ROUNDED))


def display_mongo_schema(schema: Any) -> None:
    """Render MongoDB database inspection result with collections, sampled BSON fields, and indexes."""
    # Summary header
    grid = Table.grid(padding=(0, 2))
    grid.add_column(style="bold cyan", justify="right")
    grid.add_column(style="white")

    grid.add_row("Database:", schema.database_name)
    if schema.server_version:
        grid.add_row("Server Version:", schema.server_version)
    if schema.accessible_databases:
        grid.add_row("Discovered Databases:", ", ".join(schema.accessible_databases))
    grid.add_row("Total Collections:", str(len(schema.collections)))
    grid.add_row("Sampling Limit:", f"{schema.sample_limit} documents per collection")

    console.print(
        Panel(
            grid,
            title="[bold green]Support — MongoDB Document Schema Discovery[/bold green]",
            border_style="green",
            box=box.ROUNDED,
        )
    )
    console.print(
        "[dim yellow]* Bounded document sampling: Field types and occurrence rates are inferred "
        "from sampled documents. Document structures are dynamic, not rigid DDL constraints.[/dim yellow]\n"
    )

    if not schema.collections:
        console.print(
            Panel(
                "[yellow]No collections found in this database.[/yellow]",
                title="Discovery Result",
                border_style="yellow",
            )
        )
        return

    for col_name, col in schema.collections.items():
        count_str = f"~{col.estimated_count:,} docs" if col.estimated_count is not None else "count unknown"
        sample_str = f"{col.sample_size} sampled"
        title = f"[bold cyan]Collection: {col.name}[/bold cyan] [dim]({count_str} · {sample_str})[/dim]"

        if col.is_empty:
            console.print(
                Panel(
                    "[dim yellow]Collection is empty. No documents could be sampled.[/dim yellow]",
                    title=title,
                    border_style="dim",
                    box=box.ROUNDED,
                )
            )
            console.print()
            continue

        field_table = Table(
            title=title,
            box=box.ROUNDED,
            header_style="bold cyan",
            expand=True,
        )
        field_table.add_column("Field Path", style="bold white", ratio=3)
        field_table.add_column("Observed BSON Type", style="cyan", ratio=3)
        field_table.add_column("Presence", justify="center", ratio=2)
        field_table.add_column("Optional / Nullable", justify="center", ratio=2)

        for field in col.fields:
            type_str = field.type_display
            if len(field.types) > 1:
                type_str = f"[bold magenta]{type_str}[/bold magenta]"  # Highlight polymorphic fields

            presence_str = f"{field.count}/{field.sample_size} ({field.occurrence_rate}%)"
            opt_str = "[yellow]YES[/yellow]" if field.nullable else "[green]NO[/green]"

            field_table.add_row(
                field.name,
                type_str,
                presence_str,
                opt_str,
            )

        console.print(field_table)

        if col.indexes:
            idx_lines = []
            for idx in col.indexes:
                keys_desc = ", ".join(f"{k}: {direction}" for k, direction in idx.keys)
                unique_badge = " [bold magenta][UNIQUE][/bold magenta]" if idx.unique else ""
                idx_lines.append(f"  * [bold]{idx.name}[/bold]: ({keys_desc}){unique_badge}")
            console.print(
                Panel(
                    "\n".join(idx_lines),
                    title=f"[dim]Indexes on {col.name}[/dim]",
                    border_style="dim cyan",
                    box=box.SQUARE,
                )
            )

        console.print()

