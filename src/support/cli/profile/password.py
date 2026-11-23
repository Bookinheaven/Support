"""Profile password management commands."""

import typer

from support.application.errors import (
    CredentialError,
    ProfileError,
)
from support.cli.composition import get_profile_service
from support.domain.profiles import DatabaseType, MongoDBConnection, ProfileValidationError

password_app = typer.Typer(
    name="password",
    help="Manage stored database profile passwords.",
    no_args_is_help=True,
)


@password_app.command(name="set", help="Securely store a database password for a profile.")
def set_password(
    name: str = typer.Argument(..., help="Name of the profile."),
) -> None:
    """Prompt for and securely store a database password."""
    service = get_profile_service()
    try:
        # Check profile existence and type before asking for password
        profile = service.get_profile(name)
        if profile.connection.database_type == DatabaseType.SQLITE:
            typer.echo(f"Error: Profile '{name}' uses SQLite, which does not support passwords.", err=True)
            raise typer.Exit(code=1)
        if (
            profile.connection.database_type == DatabaseType.MONGODB
            and isinstance(profile.connection, MongoDBConnection)
            and not profile.connection.username
        ):
            typer.echo(
                f"Error: Profile '{name}' does not have a username configured. "
                "MongoDB password authentication requires a username.",
                err=True,
            )
            raise typer.Exit(code=1)

        password = typer.prompt(
            f"Enter database password for profile '{name}'",
            hide_input=True,
        )
        confirm = typer.prompt(
            "Confirm database password",
            hide_input=True,
        )
        if password != confirm:
            typer.echo("Error: Passwords do not match.", err=True)
            raise typer.Exit(code=1)

        service.set_profile_password(name, password)
        typer.echo(f"Password for profile '{name}' stored securely.")
    except (ProfileValidationError, ProfileError, CredentialError) as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(code=1)


@password_app.command(name="status", help="Check whether a saved password exists for a profile.")
def password_status(
    name: str = typer.Argument(..., help="Name of the profile."),
) -> None:
    """Report whether a password exists without retrieving or printing it."""
    service = get_profile_service()
    try:
        profile = service.get_profile(name)
        if profile.connection.database_type == DatabaseType.SQLITE:
            typer.echo(f"Profile '{name}': SQLite does not use passwords.")
            return

        has_pw = service.has_profile_password(name)
        if has_pw:
            typer.echo(f"Profile '{name}': Saved password is present.")
        else:
            typer.echo(f"Profile '{name}': No saved password.")
    except (ProfileValidationError, ProfileError, CredentialError) as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(code=1)


@password_app.command(name="remove", help="Remove the saved password for a profile.")
def remove_password(
    name: str = typer.Argument(..., help="Name of the profile."),
) -> None:
    """Remove the saved password for a profile."""
    service = get_profile_service()
    try:
        service.remove_profile_password(name)
        typer.echo(f"Password for profile '{name}' removed.")
    except (ProfileValidationError, ProfileError, CredentialError) as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(code=1)
