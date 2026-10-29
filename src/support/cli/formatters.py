"""Professional, concise CLI formatting and presentation utilities."""

import sys
from typing import Optional
from rich.console import Console
from rich.table import Table
from rich import box

from support.db.models import ConnectionProfile
from support.db.health import HealthCheckResult

console = Console()


def _safe_symbol(char: str, fallback: str) -> str:
    try:
        enc = getattr(sys.stdout, "encoding", "utf-8") or "utf-8"
        char.encode(enc)
        return char
    except Exception:
        return fallback


CHECKMARK = _safe_symbol("✓", "+")
CROSS = _safe_symbol("✗", "x")
BULLET = _safe_symbol("·", "-")


def print_step(message: str) -> None:
    """Print an in-progress operational step."""
    console.print(f"[dim]{message}[/dim]")


def print_success(message: str) -> None:
    """Print a concise success line with a green checkmark."""
    console.print(f"[bold green]{CHECKMARK}[/bold green] {message}")


def print_warning(message: str) -> None:
    """Print a concise warning line with a yellow symbol."""
    console.print(f"[bold yellow]![/bold yellow] {message}")


def print_failure(title: str, details: Optional[str] = None, hint: Optional[str] = None) -> None:
    """Print a concise failure message without decorative panels or stack traces."""
    console.print(f"[bold red]{CROSS}[/bold red] {title}")
    if details:
        console.print(f"  [dim]{details}[/dim]")
    if hint:
        console.print(f"  [yellow]{hint}[/yellow]")


def render_profiles_table(
    profiles: list[ConnectionProfile],
    health_results: Optional[dict[str, HealthCheckResult]] = None,
    no_check: bool = False,
) -> None:
    """Render a compact, readable table of saved connection profiles."""
    if not profiles:
        console.print("[dim]No saved database profiles. Use 'support db add' to save a connection.[/dim]")
        return

    table = Table(
        box=box.SIMPLE,
        header_style="bold cyan",
        show_header=True,
        pad_edge=False,
    )
    table.add_column("Profile", style="bold white", no_wrap=True)
    table.add_column("Dialect", style="cyan", no_wrap=True)
    table.add_column("Endpoint", style="white")
    table.add_column("User", style="yellow")
    table.add_column("Status", justify="center", no_wrap=True)
    table.add_column("Latency", justify="right", no_wrap=True)

    for p in profiles:
        if no_check:
            status_cell = "[dim]UNCHECKED[/dim]"
            latency_cell = "-"
        else:
            res = health_results.get(p.name) if health_results else None
            status = res.status if res else "OFFLINE"
            latency = res.latency_ms if res else None

            if status == "ONLINE":
                status_cell = "[bold green]ONLINE[/bold green]"
                latency_cell = f"[green]{latency:.0f} ms[/green]" if latency is not None else "-"
            elif status == "AUTH FAILED":
                status_cell = "[bold red]AUTH FAILED[/bold red]"
                latency_cell = "-"
            elif status == "TIMEOUT":
                status_cell = "[bold yellow]TIMEOUT[/bold yellow]"
                latency_cell = "-"
            elif status == "OFFLINE":
                status_cell = "[bold red]OFFLINE[/bold red]"
                latency_cell = "-"
            else:
                status_cell = "[bold red]ERROR[/bold red]"
                latency_cell = "-"

        user_display = p.username if p.username else "-"
        table.add_row(
            p.name,
            p.dialect.display_name,
            p.display_target,
            user_display,
            status_cell,
            latency_cell,
        )

    console.print(table)
