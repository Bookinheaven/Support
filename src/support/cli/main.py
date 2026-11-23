"""Command-line interface entry point for Support."""

import sys

import typer

from support import __version__
from support.cli.profile import profile_app

APP_NAME = "support"

app = typer.Typer(
    name=APP_NAME,
    help="Support — Developer-first, terminal-native database workspace.",
    add_completion=False,
    no_args_is_help=False,
)
app.add_typer(profile_app, name="profile")


def version_callback(value: bool) -> None:
    """Print the application version and exit."""
    if value:
        typer.echo(f"{APP_NAME} version {__version__}")
        raise typer.Exit()


@app.callback(invoke_without_command=True)
def main(
    ctx: typer.Context,
    version: bool = typer.Option(
        False,
        "--version",
        "-V",
        help="Show application version and exit.",
        callback=version_callback,
        is_eager=True,
    ),
) -> None:
    """Root entry point for the Support CLI."""
    if ctx.invoked_subcommand is None:
        typer.echo(
            f"Welcome to Support (v{__version__})\n"
            "An AI-assisted database intelligence and operations workspace.\n\n"
            "Run 'support --help' to see available options."
        )


def cli() -> None:
    """Main CLI entry point callable with top-level keyboard interrupt safety."""
    try:
        app()
    except KeyboardInterrupt:
        typer.echo("\nAborted.")
        sys.exit(130)


if __name__ == "__main__":
    cli()
