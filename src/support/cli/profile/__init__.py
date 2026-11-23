"""Profile management CLI package for Support."""

import typer

from support.cli.profile.commands import (
    add_profile,
    list_profiles,
    remove_profile,
    rename_profile,
    show_profile,
)
from support.cli.profile.connection import test_profile
from support.cli.profile.password import password_app

profile_app = typer.Typer(
    name="profile",
    help="Manage database connection profiles.",
    no_args_is_help=True,
)

# Register profile CRUD commands
profile_app.command(name="add", help="Add a new database profile.")(add_profile)
profile_app.command(name="list", help="List all saved database profiles.")(list_profiles)
profile_app.command(name="show", help="Display details of a saved database profile.")(show_profile)
profile_app.command(name="rename", help="Rename an existing profile preserving its UUID.")(rename_profile)
profile_app.command(name="remove", help="Remove a saved database profile.")(remove_profile)

# Register connection test command
profile_app.command(name="test", help="Test database connection for a saved profile.")(test_profile)

# Register password sub-app
profile_app.add_typer(password_app, name="password")

__all__ = ["profile_app"]
