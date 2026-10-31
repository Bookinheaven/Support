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

                sub_input_func = input_func or (
                    (lambda p: prompt_session.prompt(p)) if prompt_session else (lambda p: console.input(p))
                )

                from support.core.performance import PerformanceTracker, set_current_tracker
                from support.core.config import AppConfig

                tracker = PerformanceTracker("user_query")
                set_current_tracker(tracker)
                tracker.start_stage("request_processing")

                try:
                    # 0. Check for cancellation of pending clarification
                    if getattr(state, "pending_clarification", None) and cleaned.lower() in ("cancel", "never mind", "nevermind", "stop", "abort"):
                        state.pending_clarification = None
                        console.print("\n[dim]Clarification cancelled. Pending request discarded.[/dim]\n")
                        state.add_message("assistant", "Clarification cancelled.")
                        continue

                    # 1. Resume pending conversational mutation if active
                    if state.pending_mutation:
                        from support.tools.executor import resume_pending_mutation

                        if resume_pending_mutation(
                            user_response=cleaned,
                            state=state,
                            db_service=dbs,
                            user_input_func=sub_input_func,
                        ):
                            continue

                    # 2. Check for deterministic tool operations (diagram, inspection, table listing)
                    # When a clarification is pending, route directly to QueryGenerator
                    if not getattr(state, "pending_clarification", None):
                        from support.tools.selector import select_tool_for_user_request
                        from support.tools.executor import execute_database_tool

                        tracker.start_stage("tool_selection")
                        try:
                            # Avoid redundant LLM call for tool selection; ordinary database requests
                            # proceed directly to QueryGenerator, while deterministic tools (diagram, inspection,
                            # schema listing) are detected without model overhead.
                            tool_call = select_tool_for_user_request(
                                message=cleaned,
                                schema=state.cached_schema,
                                profile=state.database_profile,
                                state=state,
                                llm_service=None,
                            )
                        finally:
                            tracker.end_stage("tool_selection")

                        if tool_call.tool in ("inspect_tables", "generate_diagram", "list_tables"):
                            execute_database_tool(
                                call=tool_call,
                                state=state,
                                db_service=dbs,
                                llm_service=llms,
                                user_input_func=sub_input_func,
                                original_question=cleaned,
                            )
                            continue

                    # 3. For all database requests (SELECT, CREATE, ALTER, INSERT, UPDATE, DELETE),
                    # directly ask the LLM to generate the SQL operation with a single loading indicator
                    from support.query.generator import QueryGenerator
                    from support.query.display import display_query_draft
                    from support.workspace.execution_commands import handle_conversational_execution

                    generator = QueryGenerator(db_service=dbs, llm_service=llms)
                    draft = generator.generate_draft(cleaned, state)

                    # Handle clarification response: never enter SQL validation or execution pipeline
                    if getattr(draft, "is_clarification", False):
                        console.print()
                        console.print(f"[bold cyan]{draft.clarification}[/bold cyan]")
                        console.print()

                        from support.workspace.state import PendingClarification
                        if state.pending_clarification:
                            state.pending_clarification.clarifications_history.append(
                                (state.pending_clarification.clarification_question, cleaned)
                            )
                            state.pending_clarification.clarification_question = draft.clarification
                        else:
                            state.pending_clarification = PendingClarification(
                                original_question=cleaned,
                                clarification_question=draft.clarification,
                                database_name=state.active_database,
                            )
                        state.add_message(
                            "assistant",
                            draft.clarification,
                            metadata={"clarification": draft.clarification},
                        )
                        continue

                    # If SQL draft was successfully produced, clear any pending clarification
                    if draft.sql and getattr(state, "pending_clarification", None):
                        state.pending_clarification = None

                    state.last_query_draft = draft
                    state.add_message(
                        "assistant",
                        f"Generated query draft: {draft.sql}" if draft.sql else draft.explanation,
                        metadata={"draft_sql": draft.sql, "validation_status": draft.validation_status},
                    )

                    if draft.is_validated and not getattr(draft, "unknown_tables", []):
                        try:
                            from support.history.store import QueryHistoryStore
                            QueryHistoryStore().add_entry(
                                question=draft.question,
                                sql=draft.sql,
                                database=state.active_database or "unknown",
                                dialect=draft.dialect,
                                explanation=draft.explanation,
                                is_validated=draft.is_validated,
                                validation_status=draft.validation_status,
                                is_schema_verified=draft.is_schema_verified,
                                schema_status=draft.schema_status,
                                execution_status=draft.execution_status or "Drafted (pending approval)",
                            )
                        except Exception:
                            pass

                        # For read-only queries, display single SQL preview before execution prompt
                        if draft.is_readonly:
                            display_query_draft(
                                draft,
                                db_name=state.active_database,
                                dialect_name=state.database_profile.dialect.value if state.database_profile else None,
                            )

                        # Conversational execution handles unified mutation preview or read-only execution prompt
                        handle_conversational_execution(
                            draft=draft,
                            state=state,
                            db_service=dbs,
                            llm_service=llms,
                            user_input_func=sub_input_func,
                            view_mode="table",
                        )
                    else:
                        # Validation failed or unknown tables: render concise rejection and suggestion
                        display_query_draft(
                            draft,
                            db_name=state.active_database,
                            dialect_name=state.database_profile.dialect.value if state.database_profile else None,
                        )
                finally:
                    tracker.end_stage("request_processing")
                    tracker.finish()
                    state.last_performance_metrics = tracker
                    set_current_tracker(None)
                    if AppConfig.from_env().debug:
                        console.print(f"\n[dim cyan]{tracker.format_summary_table()}[/dim cyan]\n")
                continue
    finally:
        checker.shutdown()
