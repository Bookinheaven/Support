"""Interactive terminal workspace session loop for Support."""

import sys
from typing import Callable, Optional

from prompt_toolkit import PromptSession
from prompt_toolkit.formatted_text import HTML

from support.cli.display import console
from support.db.service import DatabaseService
from support.llm.service import LLMService
from support.core.dialect import Dialect
from support.query.display import display_query_draft
from support.query.generator import QueryGenerator
from support.workspace.checker import WorkspaceStatusChecker
from support.workspace.commands import execute_slash_command
from support.workspace.display import (
    display_natural_language_placeholder,
    display_workspace_header,
    display_workspace_title,
    get_prompt_html,
)
from support.workspace.state import WorkspaceSessionState


def run_workspace_session(
    db_service: Optional[DatabaseService] = None,
    llm_service: Optional[LLMService] = None,
    input_func: Optional[Callable[[str], str]] = None,
) -> None:
    """Launch and run the interactive AI database workspace.

    Args:
        db_service: Optional DatabaseService instance (default creates one).
        llm_service: Optional LLMService instance (default creates one).
        input_func: Optional input callable for prompt retrieval (default: prompt_toolkit / console.input).
    """
    dbs = db_service or DatabaseService()
    llms = llm_service or LLMService()

    # Fast startup: Initialize workspace state from local config/profiles only
    state = WorkspaceSessionState.initialize(db_service=dbs, llm_service=llms)

    # Background verification coordinator
    checker = WorkspaceStatusChecker(state, db_service=dbs, llm_service=llms)
    checker.start_all()

    # Determine interactive mode
    is_interactive = input_func is None and sys.stdin.isatty()
    prompt_session: Optional[PromptSession] = None

    if is_interactive:
        try:
            prompt_session = PromptSession()
        except Exception:
            prompt_session = None

    # Register listener for live redraws if prompt_session is active
    if prompt_session is not None:
        def on_status_change() -> None:
            if prompt_session.app and prompt_session.app.is_running:
                try:
                    loop = prompt_session.app.loop
                    if loop and loop.is_running():
                        loop.call_soon_threadsafe(prompt_session.app.invalidate)
                except Exception:
                    pass

        state.add_listener(on_status_change)
        display_workspace_title()
    else:
        display_workspace_header(state)

    first_turn = True

    try:
        while True:
            try:
                if prompt_session is not None:
                    raw_input = prompt_session.prompt(
                        lambda: HTML(get_prompt_html(state, first_turn=first_turn))
                    )
                elif input_func is not None:
                    prompt_str = get_prompt_html(state, first_turn=first_turn) if first_turn else "> "
                    raw_input = input_func(prompt_str)
                else:
                    prompt_str = "[bold cyan]support[/bold cyan] > "
                    raw_input = console.input(prompt_str)
            except (KeyboardInterrupt, EOFError):
                console.print("\nExiting Support workspace.")
                break

            first_turn = False
            cleaned = raw_input.strip()
            if not cleaned:
                continue

            if cleaned.startswith("/"):
                if cleaned.lower() == "/clear":
                    first_turn = True

                sub_input_func = input_func or (
                    (lambda p: prompt_session.prompt(p)) if prompt_session else (lambda p: console.input(p))
                )
                should_exit = execute_slash_command(
                    command_str=cleaned,
                    state=state,
                    db_service=dbs,
                    llm_service=llms,
                    user_input_func=sub_input_func,
                    checker=checker,
                )
                if should_exit:
                    break
            else:
                state.add_message("user", cleaned)

                if not state.active_database or not state.database_profile:
                    console.print()
                    console.print("[bold yellow]No active database selected.[/bold yellow]")
                    console.print("[dim]Use [bold white]/db[/bold white] to select a database profile before asking queries.[/dim]\n")
                    state.add_message("assistant", "No active database selected. Use /db to select one.")
                    continue

                if state.database_profile.dialect == Dialect.MONGODB:
                    console.print()
                    console.print("[bold yellow]MongoDB natural-language query generation is not supported yet.[/bold yellow]")
                    console.print("[dim]Query generation is currently available for relational databases: PostgreSQL, MySQL, SQLite, and SQL Server.[/dim]\n")
                    state.add_message("assistant", "MongoDB natural-language query generation is not supported yet.")
                    continue

                import re
                norm_q = re.sub(r"[^\w\s]", "", cleaned.lower().strip())
                if norm_q in (
                    "show all the tables",
                    "show all tables",
                    "show tables",
                    "list all the tables",
                    "list all tables",
                    "list tables",
                    "what tables exist",
                    "what tables are in the database",
                    "what are all the tables",
                    "display tables",
                    "get tables",
                ):
                    from support.workspace.general_commands import handle_schema
                    handle_schema(state, db_service=dbs)
                    state.add_message("assistant", f"Displayed discovered schema tables for {state.active_database}.")
                    continue

                generator = QueryGenerator(db_service=dbs, llm_service=llms)

                with console.status("[cyan]Analyzing your question...[/cyan]", spinner="dots") as status:
                    status.update("[cyan]Inspecting relevant schema...[/cyan]")
                    status.update("[cyan]Generating SQL...[/cyan]")
                    draft = generator.generate_draft(cleaned, state)

                display_query_draft(draft)
                state.last_query_draft = draft
                state.add_message(
                    "assistant",
                    draft.explanation or "Generated query draft.",
                    metadata={
                        "sql": draft.sql,
                        "validation": draft.validation_status,
                        "execution": draft.execution_status,
                        "dialect": draft.dialect,
                    },
                )
    finally:
        checker.shutdown()
