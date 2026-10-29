"""Database profile selection and interactive onboarding for Support workspace."""

from typing import Optional
from rich import box
from rich.table import Table

from support.cli.display import console
from support.db.service import ConnectionVerificationError, DatabaseService
from support.workspace.state import WorkspaceSessionState


def _prompt_password(prompt_text: str = "Password: ", user_input_func=None) -> str:
    """Prompt for password, masking input when in interactive terminal."""
    if user_input_func is not None and user_input_func is not input and user_input_func is not console.input:
        return user_input_func(prompt_text)
    try:
        import getpass
        return getpass.getpass(prompt_text)
    except Exception:
        return input(prompt_text)


def guided_add_database_profile(
    db_service: DatabaseService,
    user_input_func=input,
    state: Optional[WorkspaceSessionState] = None,
) -> bool:
    """Provide an interactive guided setup to register a new database profile."""
    console.print()
    console.print("[bold cyan]Guided Database Setup[/bold cyan]")
    console.print("Choose configuration method:")
    console.print("  [bold white]1[/bold white]. Connection URL (e.g. postgresql://..., mongodb://..., etc.)")
    console.print("  [bold white]2[/bold white]. Step-by-step connection fields")
    console.print("  [dim]Press Enter or 'c' to cancel[/dim]")

    try:
        method = user_input_func("Select method [1/2]: ").strip()
    except (KeyboardInterrupt, EOFError):
        console.print("\n[dim]Setup cancelled.[/dim]")
        return False

    if not method or method.lower() in ("c", "cancel", "q", "quit"):
        console.print("[dim]Setup cancelled.[/dim]")
        return False

    url: Optional[str] = None
    dialect: Optional[str] = None
    host: Optional[str] = None
    port: Optional[int] = None
    database: Optional[str] = None
    username: Optional[str] = None
    password: Optional[str] = None
    profile_name: Optional[str] = None

    if method == "1":
        try:
            url_input = user_input_func("Connection URL: ").strip()
        except (KeyboardInterrupt, EOFError):
            console.print("\n[dim]Setup cancelled.[/dim]")
            return False

        if not url_input:
            console.print("[dim]No URL provided. Setup cancelled.[/dim]")
            return False
        url = url_input

        try:
            name_input = user_input_func("Profile name [Enter to infer from database]: ").strip()
            if name_input:
                profile_name = name_input
        except (KeyboardInterrupt, EOFError):
            console.print("\n[dim]Setup cancelled.[/dim]")
            return False

    elif method == "2":
        console.print("\nSupported database types:")
        console.print("  [bold white]1[/bold white]. PostgreSQL")
        console.print("  [bold white]2[/bold white]. MySQL")
        console.print("  [bold white]3[/bold white]. SQLite")
        console.print("  [bold white]4[/bold white]. Microsoft SQL Server")
        console.print("  [bold white]5[/bold white]. MongoDB")

        try:
            dialect_choice = user_input_func("Select database type [1-5]: ").strip()
        except (KeyboardInterrupt, EOFError):
            console.print("\n[dim]Setup cancelled.[/dim]")
            return False

        dialect_map = {
            "1": "postgresql",
            "2": "mysql",
            "3": "sqlite",
            "4": "mssql",
            "5": "mongodb",
            "postgres": "postgresql",
            "postgresql": "postgresql",
            "mysql": "mysql",
            "sqlite": "sqlite",
            "mssql": "mssql",
            "sqlserver": "mssql",
            "mongo": "mongodb",
            "mongodb": "mongodb",
        }
        dialect = dialect_map.get(dialect_choice.lower())
        if not dialect:
            console.print(f"[bold red]Error:[/bold red] Invalid choice '{dialect_choice}'.")
            return False

        if dialect == "sqlite":
            try:
                db_path = user_input_func("Path to SQLite database file: ").strip()
            except (KeyboardInterrupt, EOFError):
                console.print("\n[dim]Setup cancelled.[/dim]")
                return False
            if not db_path:
                console.print("[bold red]Error:[/bold red] SQLite path cannot be empty.")
                return False
            database = db_path
        else:
            default_ports = {
                "postgresql": 5432,
                "mysql": 3306,
                "mssql": 1433,
                "mongodb": 27017,
            }
            def_port = default_ports.get(dialect, 5432)

            try:
                h_input = user_input_func("Host [localhost]: ").strip()
                host = h_input or "localhost"

                p_input = user_input_func(f"Port [{def_port}]: ").strip()
                port = int(p_input) if p_input.isdigit() else def_port

                db_input = user_input_func("Database name: ").strip()
                if not db_input:
                    console.print("[bold red]Error:[/bold red] Database name cannot be empty.")
                    return False
                database = db_input

                u_input = user_input_func("Username [optional]: ").strip()
                username = u_input or None

                if username:
                    password = _prompt_password("Password: ", user_input_func)
                    if not password:
                        password = None
            except (KeyboardInterrupt, EOFError):
                console.print("\n[dim]Setup cancelled.[/dim]")
                return False

        try:
            name_input = user_input_func(f"Profile name [Enter to use '{database}']: ").strip()
            profile_name = name_input or database
        except (KeyboardInterrupt, EOFError):
            console.print("\n[dim]Setup cancelled.[/dim]")
            return False

    else:
        console.print("[bold red]Error:[/bold red] Invalid selection. Choose 1 or 2.")
        return False

    # Attempt registration with test_connection=True
    saved = False
    is_verified = False
    verification_error = None
    health_result = None
    profile = None

    with console.status("[cyan]Verifying connection...[/cyan]", spinner="dots"):
        try:
            profile, health_result = db_service.register_profile(
                url=url,
                name=profile_name,
                dialect=dialect,
                host=host,
                port=port,
                database=database,
                username=username,
                password=password,
                test_connection=True,
            )
            saved = True
            is_verified = True
        except ConnectionVerificationError as e:
            verification_error = e
        except Exception as e:
            console.print(f"[bold red]Registration error:[/bold red] {e}")
            return False

    if not saved and verification_error:
        console.print(f"[bold red]Connection failed:[/bold red] {verification_error.result.status}")
        if verification_error.result.hint:
            console.print(f"  [yellow]{verification_error.result.hint}[/yellow]")

        try:
            save_anyway = user_input_func("Save unverified profile anyway? [y/N]: ").strip().lower()
        except (KeyboardInterrupt, EOFError):
            console.print("\n[dim]Setup cancelled.[/dim]")
            return False

        if save_anyway in ("y", "yes"):
            try:
                profile, health_result = db_service.register_profile(
                    url=url,
                    name=profile_name,
                    dialect=dialect,
                    host=host,
                    port=port,
                    database=database,
                    username=username,
                    password=password,
                    test_connection=False,
                )
                saved = True
                is_verified = False
            except Exception as e:
                console.print(f"[bold red]Failed to save unverified profile:[/bold red] {e}")
                return False
        else:
            console.print("[dim]Profile not saved.[/dim]")
            return False

    if saved and profile:
        if is_verified:
            ver_part = f" · {health_result.server_version}" if health_result and health_result.server_version else ""
            lat_part = f" · {health_result.latency_ms:.0f} ms" if health_result and health_result.latency_ms is not None else ""
            console.print(f"[bold green]✓[/bold green] Connection verified{ver_part}{lat_part}.")
        else:
            console.print(f"[bold yellow]![/bold yellow] Saved unverified connection '{profile.name}'.")

        console.print(f"[bold green]✓[/bold green] Profile [bold white]{profile.name}[/bold white] registered.")

        if state is not None:
            stat_text = (
                f"ONLINE ({health_result.latency_ms:.0f} ms)"
                if is_verified and health_result and health_result.latency_ms is not None
                else ("ONLINE" if is_verified else "Configured (untested)")
            )
            state.set_database(name=profile.name, profile=profile, status=stat_text)
            console.print(f"[bold green]✓[/bold green] Set as active session database.")

    return True


def handle_db_selection(
    state: WorkspaceSessionState,
    db_service: DatabaseService,
    user_input_func=input,
    checker: Optional[object] = None,
) -> None:
    """Handle /db command: display saved database profiles and let user select or add one."""
    console.print()
    console.print("[bold cyan]Database Profiles[/bold cyan]")

    with console.status("[cyan]Checking database connections...[/cyan]", spinner="dots"):
        profiles, health_results = db_service.list_profiles_with_status(
            check_health=True, timeout=2.0
        )

    if not profiles:
        console.print("  [yellow]No database profiles configured.[/yellow]")
        console.print("  [dim]You can register a database with: support db add --url <DATABASE_URL>[/dim]")
        console.print()
        try:
            start_setup = user_input_func("Would you like to set up a database profile now? [Y/n]: ").strip().lower()
        except (KeyboardInterrupt, EOFError):
            return

        if not start_setup or start_setup in ("y", "yes"):
            guided_add_database_profile(db_service, user_input_func=user_input_func, state=state)
        return

    table = Table(
        box=box.SIMPLE,
        header_style="bold cyan",
        show_header=True,
        pad_edge=False,
    )
    table.add_column("#", justify="right", style="dim", no_wrap=True)
    table.add_column("Profile", style="bold white", no_wrap=True)
    table.add_column("Dialect", style="cyan", no_wrap=True)
    table.add_column("Endpoint", style="white", no_wrap=True)
    table.add_column("Status", justify="center", no_wrap=True)

    for idx, p in enumerate(profiles, start=1):
        is_active = p.name == state.active_database
        prefix = "[bold green]*[/bold green] " if is_active else "  "
        profile_cell = f"{prefix}{p.name}"

        h = health_results.get(p.name)
        if h and h.is_online:
            lat = f" ({h.latency_ms:.0f} ms)" if h.latency_ms is not None else ""
            status_cell = f"[bold green]ONLINE[/bold green]{lat}"
        elif h and "TIMEOUT" in str(getattr(h.status, "value", h.status)):
            status_cell = "[bold yellow]TIMEOUT[/bold yellow]"
        elif h:
            st = str(getattr(h.status, "value", h.status))
            status_cell = f"[bold red]{st}[/bold red]"
        else:
            status_cell = "[dim]UNKNOWN[/dim]"

        table.add_row(
            str(idx),
            profile_cell,
            p.dialect.value,
            p.display_target,
            status_cell,
        )

    console.print(table)
    console.print("[dim]* Active session database  |  [A] Add a new profile[/dim]")
    console.print()

    # Prompt user for selection
    try:
        choice = user_input_func(
            f"Select database [1-{len(profiles)}, 'a' to add new, or Enter to keep '{state.active_database or 'none'}']: "
        ).strip()
    except (KeyboardInterrupt, EOFError):
        console.print("\n[dim]Selection cancelled.[/dim]")
        return

    if not choice:
        console.print("[dim]Database selection unchanged.[/dim]")
        return

    if choice.lower() in ("a", "add"):
        guided_add_database_profile(db_service, user_input_func=user_input_func, state=state)
        return

    # Match by index number or profile name
    chosen_profile = None
    if choice.isdigit():
        idx = int(choice)
        if 1 <= idx <= len(profiles):
            chosen_profile = profiles[idx - 1]
    else:
        for p in profiles:
            if p.name.lower() == choice.lower():
                chosen_profile = p
                break

    if not chosen_profile:
        console.print(f"[bold red]Error:[/bold red] Unknown database profile '{choice}'.")
        return

    # Check connection status of chosen profile
    h_res = health_results.get(chosen_profile.name)
    status_str = "Status unknown"
    if h_res:
        status_str = str(getattr(h_res.status, "value", h_res.status))
        if h_res.latency_ms is not None:
            status_str += f" ({h_res.latency_ms:.0f} ms)"

    state.set_database(
        name=chosen_profile.name,
        profile=chosen_profile,
        status=status_str,
    )
    if checker and hasattr(checker, "check_database"):
        checker.check_database()

    console.print(
        f"[bold green]✓[/bold green] Active database set to [bold white]{chosen_profile.name}[/bold white] "
        f"[dim]({chosen_profile.dialect.value})[/dim]."
    )
