"""Command-line interface entry point for Support."""

import sys
from typing import Optional

import typer
from rich.console import Console

from support import __version__

APP_NAME = "support"
console = Console()

app = typer.Typer(
    name=APP_NAME,
    help="Support — Developer-First Terminal Database Workspace.",
    add_completion=False,
    no_args_is_help=False,
)


def version_callback(value: bool) -> None:
    """Print project version and exit."""
    if value:
        console.print(f"[bold cyan]{APP_NAME}[/bold cyan] version [bold white]{__version__}[/bold white]")
        raise typer.Exit()


@app.callback(invoke_without_command=True)
def main(
    ctx: typer.Context,
    version: Optional[bool] = typer.Option(
        None,
        "--version",
        "-v",
        "-V",
        help="Show project version and exit.",
        callback=version_callback,
        is_eager=True,
    ),
) -> None:
    """Support CLI root entry point."""
    if ctx.invoked_subcommand is None:
        try:
            console.print(
                f"[bold cyan]Welcome to Support[/bold cyan] [dim](v{__version__})[/dim] — "
                "Terminal Database Workspace\n"
                "Use [bold white]support --help[/bold white] for available options."
            )
        except KeyboardInterrupt:
            console.print("\n[dim]Aborted.[/dim]")
            raise typer.Exit(code=130)


def cli() -> None:
    """Entry point wrapper with top-level KeyboardInterrupt handling."""
    try:
        app()
    except KeyboardInterrupt:
        console.print("\n[dim]Aborted.[/dim]")
        sys.exit(130)


if __name__ == "__main__":
    cli()
