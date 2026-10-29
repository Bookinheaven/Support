"""Slash command dispatcher for the interactive Support workspace."""

from typing import Optional

from support.cli.display import console
from support.db.service import DatabaseService
from support.llm.service import LLMService
from support.workspace.db_commands import (
    _prompt_password,
    guided_add_database_profile,
    handle_db_selection,
)
from support.workspace.execution_commands import handle_run_command
from support.workspace.general_commands import (
    handle_clear,
    handle_details,
    handle_help,
    handle_schema,
    handle_status,
    handle_view,
)
from support.workspace.model_commands import handle_model_selection
from support.workspace.state import WorkspaceSessionState

__all__ = [
    "execute_slash_command",
    "guided_add_database_profile",
    "handle_clear",
    "handle_db_selection",
    "handle_details",
    "handle_help",
    "handle_model_selection",
    "handle_run_command",
    "handle_schema",
    "handle_status",
    "handle_view",
    "_prompt_password",
]


def execute_slash_command(
    command_str: str,
    state: WorkspaceSessionState,
    db_service: Optional[DatabaseService] = None,
    llm_service: Optional[LLMService] = None,
    user_input_func=input,
    checker: Optional[object] = None,
) -> bool:
    """Execute a slash command in the workspace.

    Returns:
        True if the session should exit; False to continue the session loop.
    """
    cmd_clean = command_str.strip().lower()
    cmd_parts = cmd_clean.split()
    base_cmd = cmd_parts[0] if cmd_parts else ""

    dbs = db_service or DatabaseService()
    llms = llm_service or LLMService()

    if base_cmd in ("/exit", "/quit"):
        console.print("Exiting Support workspace.")
        return True

    elif base_cmd == "/help":
        handle_help(state)
        return False

    elif base_cmd == "/status":
        sub_arg = cmd_parts[1] if len(cmd_parts) > 1 else ""
        handle_status(state, checker=checker, arg=sub_arg)
        return False

    elif base_cmd == "/schema":
        sub_arg = cmd_parts[1] if len(cmd_parts) > 1 else ""
        handle_schema(state, db_service=dbs, arg=sub_arg)
        return False

    elif base_cmd == "/details":
        handle_details(state)
        return False

    elif base_cmd == "/view":
        sub_arg = cmd_parts[1] if len(cmd_parts) > 1 else "auto"
        handle_view(state, arg=sub_arg)
        return False

    elif base_cmd == "/clear":
        handle_clear(state)
        return False

    elif base_cmd == "/db":
        handle_db_selection(state, dbs, user_input_func=user_input_func, checker=checker)
        return False

    elif base_cmd == "/model":
        handle_model_selection(state, llms, user_input_func=user_input_func, checker=checker)
        return False

    elif base_cmd == "/run":
        sub_arg = cmd_parts[1] if len(cmd_parts) > 1 else "auto"
        handle_run_command(state, db_service=dbs, user_input_func=user_input_func, view_mode=sub_arg)
        return False

    else:
        console.print(
            f"[bold red]Unknown command:[/bold red] '{command_str}'. "
            f"[dim]Type [bold white]/help[/bold white] for available commands.[/dim]"
        )
        return False
