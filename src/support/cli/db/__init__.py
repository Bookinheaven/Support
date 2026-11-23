"""Database management CLI commands and interactive workflows."""

import sys
import typer

from support.cli.db.interactive import run_db_management
from support.cli.profile.commands import (
    add_profile,
    list_profiles,
    remove_profile,
    rename_profile,
    show_profile,
)
from support.cli.profile.connection import test_profile
from support.cli.profile.password import password_app

db_app = typer.Typer(
    name="db",
    help=(
        "Manage registered database connections.\n\n"
        "Examples:\n"
        "  support db add\n"
        "  support db list\n"
        "  support db test production\n"
        "  support db remove staging"
    ),
    no_args_is_help=False,
)


def _is_interactive_terminal() -> bool:
    """Check whether standard input and output are interactive terminals."""
    return sys.stdin.isatty() and sys.stdout.isatty()


@db_app.callback(invoke_without_command=True)
def db_main(ctx: typer.Context) -> None:
    """Manage registered database connections."""
    if ctx.invoked_subcommand is None:
        if _is_interactive_terminal():
            run_db_management()
        else:
            typer.echo(
                "Manage registered database connections.\n\n"
                "Examples:\n"
                "  support db add\n"
                "  support db list\n"
                "  support db test production\n"
                "  support db remove staging\n\n"
                "Run 'support db --help' for options."
            )


# Register subcommands for direct CLI execution
db_app.command(name="add", help="Add a new database connection.")(add_profile)
db_app.command(name="list", help="List registered database connections.")(list_profiles)
db_app.command(name="test", help="Test connection for a saved profile.")(test_profile)
db_app.command(name="remove", help="Remove a database connection.")(remove_profile)
db_app.command(name="show", help="Display details of a saved profile.")(show_profile)
db_app.command(name="rename", help="Rename an existing profile.")(rename_profile)
db_app.add_typer(password_app, name="password")

__all__ = ["db_app"]
