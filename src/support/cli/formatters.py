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


def render_models_table(
    models_by_provider: dict[str, list],
    validation_results: dict[str, object],
    active_provider: str,
    active_model: str,
) -> None:
    """Render a compact table listing available and configured LLM models."""
    table = Table(
        box=box.SIMPLE,
        header_style="bold cyan",
        show_header=True,
        pad_edge=False,
    )
    table.add_column("Provider", style="bold white", no_wrap=True)
    table.add_column("Model ID", style="cyan", no_wrap=True)
    table.add_column("Status", justify="center", no_wrap=True)
    table.add_column("Details", style="white")

    for prov_name, models in models_by_provider.items():
        v_res = validation_results.get(prov_name)
        status = getattr(v_res, "status", "UNKNOWN") if v_res else "UNKNOWN"
        status_str = str(getattr(status, "value", status))

        if status_str == "AVAILABLE":
            status_cell = "[bold green]AVAILABLE[/bold green]"
        elif status_str == "NOT CONFIGURED":
            status_cell = "[bold yellow]NOT CONFIGURED[/bold yellow]"
        elif status_str == "UNREACHABLE":
            status_cell = "[bold red]UNREACHABLE[/bold red]"
        elif status_str == "AUTH FAILED":
            status_cell = "[bold red]AUTH FAILED[/bold red]"
        elif status_str == "MODEL UNAVAILABLE":
            status_cell = "[bold yellow]MODEL UNAVAILABLE[/bold yellow]"
        else:
            status_cell = f"[dim]{status_str}[/dim]"

        if not models:
            table.add_row(
                prov_name.capitalize(),
                "[dim](none)[/dim]",
                status_cell,
                getattr(v_res, "message", "-") if v_res else "-",
            )
            continue

        for i, m in enumerate(models):
            is_active = (
                prov_name.lower() == active_provider.lower()
                and m.id.lower() == active_model.lower()
            )
            model_marker = f"[bold green]*[/bold green] {m.id}" if is_active else f"  {m.id}"
            prov_label = prov_name.capitalize() if i == 0 else ""
            details = m.display_size if hasattr(m, "display_size") and m.display_size != "-" else (m.description or "-")

            table.add_row(
                prov_label,
                model_marker,
                status_cell if i == 0 else "",
                details,
            )

    console.print(table)
    console.print("[dim]* Active configured model[/dim]")


def render_current_model_panel(
    provider: str,
    model: str,
    validation: object,
    config: object,
) -> None:
    """Render a clean summary of the currently active model and settings."""
    status = getattr(validation, "status", "UNKNOWN")
    status_str = str(getattr(status, "value", status))
    latency = getattr(validation, "latency_ms", None)

    if status_str == "AVAILABLE":
        stat_color = "green"
        lat_str = f" · {latency:.0f} ms" if latency is not None else ""
        stat_line = f"[{stat_color}]{status_str}{lat_str}[/{stat_color}]"
    else:
        stat_color = "red" if "FAILED" in status_str or "UNREACHABLE" in status_str else "yellow"
        stat_line = f"[{stat_color}]{status_str}[/{stat_color}] · {getattr(validation, 'message', '')}"

    console.print(f"[bold cyan]Active Provider:[/bold cyan] [bold white]{provider}[/bold white]")
    console.print(f"[bold cyan]Active Model:[/bold cyan]    [bold white]{model}[/bold white]")
    console.print(f"[bold cyan]Status:[/bold cyan]          {stat_line}")
    if getattr(validation, "hint", None):
        console.print(f"  [yellow]{getattr(validation, 'hint')}[/yellow]")

    temp = getattr(config, "temperature", 0.1)
    timeout = getattr(config, "timeout", 30.0)
    console.print(f"[dim]Settings: temperature={temp} · timeout={timeout}s[/dim]")


def render_auth_status_table(status_data: list[dict[str, str]]) -> None:
    """Render a clean table of cloud providers and their authentication status."""
    table = Table(
        box=box.SIMPLE,
        header_style="bold cyan",
        show_header=True,
        pad_edge=False,
    )
    table.add_column("Provider", style="bold white", no_wrap=True)
    table.add_column("Status", justify="center", no_wrap=True)
    table.add_column("Storage / Source", style="white", no_wrap=True)
    table.add_column("Details", style="dim white")

    for item in status_data:
        prov = item["provider"]
        status = item["status"]
        source = item["source"]
        details = item.get("details", "")

        if status == "CONFIGURED":
            status_cell = "[bold green]CONFIGURED[/bold green]"
        else:
            status_cell = "[bold yellow]NOT CONFIGURED[/bold yellow]"

        table.add_row(prov, status_cell, source, details)

    console.print(table)

