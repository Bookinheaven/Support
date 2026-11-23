"""Interactive terminal workspace for Support."""

from __future__ import annotations

import sys
import time
from prompt_toolkit import PromptSession
from prompt_toolkit.history import InMemoryHistory
import questionary
import typer

from support.application.errors import (
    AmbiguousObjectNameError,
    AuthenticationFailedError,
    CredentialError,
    DatabaseConnectionError,
    ObjectNotFoundError,
    ProfileError,
    SchemaIntrospectionError,
)
from support.cli.composition import (
    get_assistant_workflow,
    get_connection_workflow,
    get_llm_provider,
    get_profile_service,
    get_schema_workflow,
)
from support.cli.db.interactive import compact_add_connection
from support.cli.presentation import (
    DatabaseStatusInfo,
    format_duration,
    format_endpoint,
    format_engine_name,
    get_console,
    print_status,
    render_database_status_line,
    render_model_status_line,
    render_object_description,
    render_relationships,
    render_schema_overview,
    render_tables,
)
from support.domain.schema import DatabaseSchema, TableMetadata


def clear_screen() -> None:
    """Clear terminal screen."""
    if hasattr(typer, "_click") and hasattr(typer._click, "clear"):
        typer._click.clear()
    else:
        sys.stdout.write("\033[H\033[2J\033[3J")
        sys.stdout.flush()


def _verify_profile_connectivity(profile_name: str) -> DatabaseStatusInfo:
    """Verify profile connectivity safely on startup without crashing if unreachable."""
    if not profile_name or profile_name == "(none)":
        return DatabaseStatusInfo(profile_name="(none)", is_online=False)

    workflow = get_connection_workflow()
    try:
        profile = workflow.get_profile(profile_name)
    except Exception as exc:
        return DatabaseStatusInfo(
            profile_name=profile_name,
            is_online=False,
            failure_reason=str(exc),
        )

    conn = profile.connection
    engine_name = format_engine_name(conn.database_type)
    endpoint = format_endpoint(conn)
    database_name = getattr(conn, "database", None) or getattr(conn, "database_path", None)

    password = None
    used_saved = False
    try:
        password = workflow.resolve_saved_password(profile.id)
        used_saved = password is not None
    except Exception:
        used_saved = False

    t0 = time.perf_counter()
    try:
        res = workflow.test_profile_connection(
            profile_name,
            password=password,
            used_saved_password=used_saved,
        )
        duration = time.perf_counter() - t0
        server_ver = f"{engine_name} {res.server_version}" if res.server_version else None
        return DatabaseStatusInfo(
            profile_name=profile_name,
            engine_name=engine_name,
            database_name=res.database or database_name,
            endpoint=endpoint,
            server_version=server_ver,
            is_online=res.success,
            duration_seconds=duration,
        )
    except (DatabaseConnectionError, AuthenticationFailedError, ProfileError, CredentialError) as exc:
        duration = time.perf_counter() - t0
        return DatabaseStatusInfo(
            profile_name=profile_name,
            engine_name=engine_name,
            database_name=database_name,
            endpoint=endpoint,
            is_online=False,
            duration_seconds=duration,
            failure_reason=str(exc),
        )
    except Exception as exc:
        duration = time.perf_counter() - t0
        return DatabaseStatusInfo(
            profile_name=profile_name,
            engine_name=engine_name,
            database_name=database_name,
            endpoint=endpoint,
            is_online=False,
            duration_seconds=duration,
            failure_reason=str(exc),
        )


def _show_slash_help(console: Any) -> None:
    """Display the workspace guide for slash commands and natural language queries."""
    guide = (
        "\nSupport — Workspace Guide\n\n"
        "Ask naturally:\n"
        "  Show all tables in this database\n"
        "  Describe the orders table\n"
        "  What tables reference users?\n"
        "  Explain the relationship between orders and items\n"
        "  Summarize the database structure\n\n"
        "Slash Commands:\n"
        "  /help                 Show this guide\n"
        "  /status               Show database and model status\n"
        "  /clear                Clear screen and conversation history\n"
        "  /tables               List database objects\n"
        "  /schema               Show database structure\n"
        "  /describe <object>    Inspect an object\n"
        "  /relationships        Show declared relationships\n"
        "  /exit                 Exit Support\n\n"
        "Model configuration:\n"
        "  support model\n\n"
        "Database management:\n"
        "  support db\n\n"
        "All input without a leading '/' is sent to the AI assistant.\n"
    )
    console.print(guide)


def run_workspace() -> None:
    """Launch the interactive Support workspace."""
    console = get_console()
    service = get_profile_service()
    profiles = service.list_profiles()
    active_profile = "(none)"

    if not profiles:
        console.print("No database connections configured.\n")
        try:
            add_first = questionary.confirm("Add your first database?", default=True).ask()
        except (KeyboardInterrupt, EOFError):
            add_first = False

        if add_first:
            new_profile = compact_add_connection()
            if new_profile:
                active_profile = new_profile
            else:
                active_profile = "(none)"
        else:
            active_profile = "(none)"
    elif len(profiles) == 1:
        active_profile = profiles[0].name
    else:
        # Multiple profiles exist: compact interactive selector
        choices = [p.name for p in profiles]
        try:
            selected = questionary.select(
                "Select active database connection:",
                choices=choices,
            ).ask()
        except (KeyboardInterrupt, EOFError):
            selected = None
        active_profile = selected if selected else profiles[0].name

    # Check connection with spinner if in interactive terminal
    status_info: DatabaseStatusInfo
    if active_profile != "(none)":
        if console.is_terminal and sys.stdout.isatty():
            with console.status("Checking database connection...", spinner="dots"):
                status_info = _verify_profile_connectivity(active_profile)
        else:
            status_info = _verify_profile_connectivity(active_profile)
    else:
        status_info = DatabaseStatusInfo(profile_name="(none)", is_online=False)

    # Check model availability and initialize assistant
    llm_provider = get_llm_provider()
    model_info = llm_provider.check_model_status()
    assistant_wf = get_assistant_workflow(llm_provider=llm_provider)

    # Startup context
    console.print("\nSupport\n")
    console.print(render_database_status_line(status_info))
    if model_info.name:
        console.print(render_model_status_line(model_info.name, model_info.is_available, model_info.details))
    else:
        console.print(render_model_status_line())
    console.print()

    # Initialize terminal prompt session with in-memory history for interactive TTY
    prompt_session: PromptSession[str] | None = (
        PromptSession(history=InMemoryHistory()) if sys.stdin.isatty() else None
    )

    # Interactive workspace REPL loop
    while True:
        try:
            if prompt_session is not None:
                raw_input = prompt_session.prompt("support > ")
            else:
                raw_input = input("support > ")
        except (KeyboardInterrupt, EOFError):
            console.print("\nGoodbye!")
            break

        stripped = raw_input.strip()
        if not stripped:
            continue

        if stripped.startswith("/"):
            parts = stripped.split(maxsplit=1)
            cmd_token = parts[0].lower()
            arg = parts[1].strip() if len(parts) > 1 else ""

            if cmd_token in ("/exit", "/quit", "/q"):
                console.print("Goodbye!")
                break
            elif cmd_token == "/help":
                _show_slash_help(console)
            elif cmd_token == "/status":
                print_status(console, status_info, model_info)
            elif cmd_token == "/clear":
                clear_screen()
                assistant_wf.reset_conversation()
            elif cmd_token == "/tables":
                if active_profile == "(none)":
                    console.print("No active database connection.\n\nUse 'support db' to manage database connections.\n")
                else:
                    try:
                        schema_wf = get_schema_workflow()
                        if console.is_terminal and sys.stdout.isatty():
                            with console.status("Inspecting database tables...", spinner="dots"):
                                schema = schema_wf.get_schema(active_profile)
                        else:
                            schema = schema_wf.get_schema(active_profile)
                        render_tables(console, schema)
                    except Exception as exc:
                        console.print(f"Error inspecting tables: {exc}\n")
            elif cmd_token == "/schema":
                if active_profile == "(none)":
                    console.print("No active database connection.\n\nUse 'support db' to manage database connections.\n")
                else:
                    try:
                        schema_wf = get_schema_workflow()
                        if console.is_terminal and sys.stdout.isatty():
                            with console.status("Inspecting database schema...", spinner="dots"):
                                schema = schema_wf.get_schema(active_profile)
                        else:
                            schema = schema_wf.get_schema(active_profile)
                        render_schema_overview(console, schema)
                    except Exception as exc:
                        console.print(f"Error inspecting schema: {exc}\n")
            elif cmd_token == "/describe":
                if not arg:
                    console.print("Usage: /describe <object-name>\nExample: /describe users\n")
                elif active_profile == "(none)":
                    console.print("No active database connection.\n\nUse 'support db' to manage database connections.\n")
                else:
                    try:
                        schema_wf = get_schema_workflow()
                        profile = schema_wf.get_profile(active_profile)
                        engine = profile.connection.database_type.value
                        if console.is_terminal and sys.stdout.isatty():
                            with console.status(f"Describing '{arg}'...", spinner="dots"):
                                tbl_meta = schema_wf.describe_object(active_profile, arg)
                        else:
                            tbl_meta = schema_wf.describe_object(active_profile, arg)
                        render_object_description(console, tbl_meta, engine)
                    except (ObjectNotFoundError, AmbiguousObjectNameError) as exc:
                        console.print(f"Error: {exc}\n")
                    except Exception as exc:
                        console.print(f"Error describing '{arg}': {exc}\n")
            elif cmd_token == "/relationships":
                if active_profile == "(none)":
                    console.print("No active database connection.\n\nUse 'support db' to manage database connections.\n")
                else:
                    try:
                        schema_wf = get_schema_workflow()
                        if console.is_terminal and sys.stdout.isatty():
                            with console.status("Inspecting database relationships...", spinner="dots"):
                                schema = schema_wf.get_schema(active_profile)
                        else:
                            schema = schema_wf.get_schema(active_profile)
                        render_relationships(console, schema)
                    except Exception as exc:
                        console.print(f"Error inspecting relationships: {exc}\n")
            else:
                console.print(f"\nUnknown command: {parts[0]}\n\nType /help to see available commands.\n")
        else:
            # All non-slash inputs are routed directly to the Ollama conversational assistant
            if not model_info.name:
                console.print(
                    "No assistant model configured.\n\n"
                    "Configure a model using: support model set assistant <model-name>\n"
                    "List available models with: support model list\n"
                )
            else:
                if console.is_terminal and sys.stdout.isatty():
                    with console.status("Thinking...", spinner="dots"):
                        turn_res = assistant_wf.handle_user_message(raw_input, active_profile=active_profile)
                else:
                    turn_res = assistant_wf.handle_user_message(raw_input, active_profile=active_profile)

                if turn_res.is_error:
                    console.print(f"[red]Error:[/red] {turn_res.error_message or 'Assistant failed to process request.'}\n")
                else:
                    # 1. Output any tool execution errors
                    for tool_res in turn_res.tool_results:
                        if tool_res.is_error:
                            err_msg = tool_res.data.get("error", tool_res.message)
                            console.print(f"[dim]{err_msg}[/dim]\n")

                    # 2. Path B: Direct data retrieval renders authoritative structured data
                    if turn_res.response_mode == "display":
                        opts = turn_res.presentation_options or {}
                        include_schema = opts.get("include_schema", not opts.get("omit_schema", False))
                        numbered = opts.get("numbered", False)
                        names_only = opts.get("names_only", False)
                        alphabetical = opts.get("alphabetical", False)

                        for tool_res in turn_res.tool_results:
                            if not tool_res.is_error:
                                # Merge tool-level presentation options if present
                                tool_opts = getattr(tool_res, "presentation_options", {}) or {}
                                eff_include_schema = tool_opts.get("include_schema", include_schema)
                                eff_numbered = tool_opts.get("numbered", numbered)
                                eff_names_only = tool_opts.get("names_only", names_only)
                                eff_alphabetical = tool_opts.get("alphabetical", alphabetical)

                                if tool_res.name == "list_database_objects" and isinstance(tool_res.raw_object, DatabaseSchema):
                                    render_tables(
                                        console,
                                        tool_res.raw_object,
                                        include_schema=eff_include_schema,
                                        numbered=eff_numbered,
                                        names_only=eff_names_only,
                                        alphabetical=eff_alphabetical,
                                    )
                                elif tool_res.name == "get_database_overview" and isinstance(tool_res.raw_object, DatabaseSchema):
                                    render_schema_overview(console, tool_res.raw_object)
                                elif tool_res.name == "describe_object" and isinstance(tool_res.raw_object, TableMetadata):
                                    eng = format_engine_name(tool_res.data.get("engine", "database"))
                                    render_object_description(console, tool_res.raw_object, eng)
                                elif tool_res.name == "get_relationships" and isinstance(tool_res.raw_object, DatabaseSchema):
                                    render_relationships(
                                        console,
                                        tool_res.raw_object,
                                        names_only=eff_names_only,
                                    )

                    # 3. Path A (conversational) & Path C (explanation): Display conversational text
                    if turn_res.response_text:
                        console.print(turn_res.response_text + "\n")
