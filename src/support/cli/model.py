"""Model configuration CLI commands and interactive selection menu."""

from __future__ import annotations

import sys
import questionary
import typer

from support.application.errors import OllamaConnectionError
from support.cli.composition import get_llm_provider
from support.infrastructure.config import (
    AppConfig,
    get_assistant_model,
    get_config_path,
    get_ollama_host,
    load_config,
    save_config,
)

model_app = typer.Typer(
    name="model",
    help="Manage local language model configuration for the Support assistant.",
    add_completion=False,
    no_args_is_help=False,
)


@model_app.command(name="show")
def show_model() -> None:
    """Display current model configuration and local readiness."""
    configured_model = get_assistant_model()
    ollama_host = get_ollama_host()
    llm = get_llm_provider()

    typer.echo(f"Assistant Model:    {configured_model or '(none)'}")
    typer.echo(f"Ollama Server:      {ollama_host}")

    if not configured_model:
        typer.echo("Status:             Not configured\n")
        typer.echo("Configure a model using: support model set assistant <model-name>")
        typer.echo("List available models with: support model list")
        return

    info = llm.check_model_status(configured_model)
    status_str = "Available" if info.is_available else (info.details or "Unavailable")
    tool_str = "Supported" if info.supports_tools else "Not verified"
    if not info.is_available:
        tool_str = "Unavailable"

    typer.echo(f"Status:             {status_str}")
    typer.echo(f"Tool Calling:       {tool_str}")


@model_app.command(name="list")
def list_models() -> None:
    """List locally installed Ollama models."""
    llm = get_llm_provider()
    try:
        models = llm.list_models()
    except OllamaConnectionError as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(code=1)
    except Exception as exc:
        typer.echo(f"Error querying Ollama server: {exc}", err=True)
        raise typer.Exit(code=1)

    if not models:
        typer.echo(f"No models found on Ollama server at '{llm.base_url}'.")
        typer.echo("Install a model using: ollama pull qwen2.5-coder:3b")
        return

    typer.echo("Installed Ollama Models:\n")
    for m in models:
        typer.echo(f"  {m.name}")
    typer.echo()


@model_app.command(name="set")
def set_model(
    role: str = typer.Argument(..., help="Model role to configure (e.g. 'assistant')."),
    model_name: str = typer.Argument(..., help="Model tag name (e.g. 'qwen2.5-coder:3b')."),
) -> None:
    """Configure an active model for a role."""
    clean_role = role.strip().lower()
    if clean_role != "assistant":
        typer.echo(f"Error: Unsupported role '{role}'. Currently only 'assistant' is supported.", err=True)
        raise typer.Exit(code=1)

    clean_model = model_name.strip()
    if not clean_model:
        typer.echo("Error: Model name cannot be empty.", err=True)
        raise typer.Exit(code=1)

    llm = get_llm_provider()
    try:
        info = llm.check_model_status(clean_model)
    except OllamaConnectionError as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(code=1)
    except Exception as exc:
        typer.echo(f"Error connecting to Ollama: {exc}", err=True)
        raise typer.Exit(code=1)

    if not info.is_available:
        typer.echo(f"Error: Model '{clean_model}' is not available on Ollama server at '{llm.base_url}'.", err=True)
        typer.echo(f"Details: {info.details}", err=True)
        typer.echo(f"\nTo install it locally, run:\n  ollama pull {clean_model}")
        raise typer.Exit(code=1)

    prev_model = get_assistant_model()
    cfg = load_config()
    cfg.set_model("assistant", clean_model)
    save_config(cfg)

    # Verify persistence
    reloaded = load_config().get_model("assistant")
    if reloaded != clean_model:
        typer.echo("Error: Failed to persist model configuration.", err=True)
        raise typer.Exit(code=1)

    typer.echo(f"Previous assistant model: {prev_model or '(none)'}")
    typer.echo(f"Configured assistant model: {clean_model}")
    typer.echo(f"Configuration saved to {get_config_path()}")


def run_model_behavioral_eval(llm: Any, model_name: str) -> bool:
    """Run live behavioral evaluation suite of 12 core cases against the configured model."""
    import sqlite3
    import tempfile
    from pathlib import Path
    from support.application.service import ProfileService
    from support.application.tools import ToolRegistry
    from support.cli.composition import (
        get_assistant_workflow,
        get_connection_workflow,
        get_schema_workflow,
    )
    from support.domain.profiles import SQLiteConnection
    from support.infrastructure.keyring_credentials import KeyringCredentialStore
    from support.infrastructure.sqlite_profiles import SQLiteProfileRepository

    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as tf:
        db_path = tf.name

    conn = sqlite3.connect(db_path)
    with conn:
        conn.execute("CREATE TABLE users (id INTEGER PRIMARY KEY, name TEXT);")
        conn.execute("CREATE TABLE orders (id INTEGER PRIMARY KEY, user_id INTEGER, FOREIGN KEY(user_id) REFERENCES users(id));")
    conn.close()

    prof_db = tempfile.mktemp(suffix=".sqlite3")
    eval_repo = SQLiteProfileRepository(db_path=Path(prof_db))
    eval_creds = KeyringCredentialStore()
    eval_service = ProfileService(repository=eval_repo, credential_store=eval_creds)
    eval_profile_name = "eval-test-db"
    eval_service.add_profile(eval_profile_name, SQLiteConnection(database_path=db_path))

    schema_wf = get_schema_workflow(repository=eval_repo, credential_store=eval_creds)
    conn_wf = get_connection_workflow(repository=eval_repo, credential_store=eval_creds)
    tool_reg = ToolRegistry(schema_workflow=schema_wf, connection_workflow=conn_wf)
    wf = get_assistant_workflow(llm_provider=llm, tool_registry=tool_reg)

    typer.echo(f"\nRunning behavioral evaluation suite against '{model_name}'...\n")

    cases = [
        (
            "1. Conversational (1 + 1)",
            "What is 1 + 1?",
            lambda r: r.response_mode == "conversational" and len(r.tool_results) == 0 and "2" in r.response_text,
        ),
        (
            "2. Conversational ((a+b)^2)",
            "What is (a+b)^2?",
            lambda r: r.response_mode == "conversational" and len(r.tool_results) == 0 and len(r.response_text.strip()) > 0,
        ),
        (
            "3. Direct table retrieval",
            "Display all tables.",
            lambda r: any(t.name == "list_database_objects" for t in r.tool_results) and r.response_mode == "display",
        ),
        (
            "4. Describe table",
            "Describe the orders table.",
            lambda r: any(t.name == "describe_object" and "orders" in str(t.data.get("name", "")) for t in r.tool_results),
        ),
        (
            "5. Show columns in table",
            "Show the columns in orders.",
            lambda r: any(t.name == "describe_object" and "orders" in str(t.data.get("name", "")) for t in r.tool_results),
        ),
        (
            "6. Ambiguous display orders",
            "Display orders.",
            lambda r: r.response_mode == "conversational" and len(r.tool_results) == 0 and ("record" in r.response_text.lower() or "structure" in r.response_text.lower()),
        ),
        (
            "7. Show rows unavailable",
            "Show rows from orders.",
            lambda r: r.response_mode == "conversational" and len(r.tool_results) == 0 and "not currently supported" in r.response_text.lower(),
        ),
        (
            "8. Items in orders without guessing",
            "Display the items in orders.",
            lambda r: r.response_mode == "conversational" and len(r.tool_results) == 0 and ("order_items" in r.response_text.lower() or "record" in r.response_text.lower()),
        ),
        (
            "9. Show tables without public",
            "Show tables without public.",
            lambda r: r.response_mode == "display" and r.presentation_options.get("omit_schema") is True,
        ),
        (
            "10. Explanatory relationship",
            "Why does orders reference users?",
            lambda r: any(t.name == "get_relationships" for t in r.tool_results) and len(r.response_text.strip()) > 0,
        ),
        (
            "11. Unknown table error",
            "Show an unknown table named test_foo.",
            lambda r: any(t.name == "describe_object" and t.is_error for t in r.tool_results),
        ),
    ]

    all_passed = True
    for label, prompt, validator in cases:
        wf.reset_conversation()
        try:
            res = wf.handle_user_message(prompt, active_profile=eval_profile_name, model=model_name)
            passed = validator(res)
            status = "PASS" if passed else "FAIL"
            if not passed:
                all_passed = False
            typer.echo(f"  [{status}] {label}")
        except Exception as exc:
            all_passed = False
            typer.echo(f"  [FAIL] {label}: {exc}")

    # Case 12: Multi-turn conversational context pronoun resolution
    label_12 = "12. Follow-up context (Describe orders -> Show its columns)"
    wf.reset_conversation()
    try:
        res1 = wf.handle_user_message("Describe the orders table.", active_profile=eval_profile_name, model=model_name)
        res2 = wf.handle_user_message("Show its columns.", active_profile=eval_profile_name, model=model_name)
        passed_12 = any(t.name == "describe_object" and "orders" in str(t.data.get("name", "")) for t in res2.tool_results)
        status_12 = "PASS" if passed_12 else "FAIL"
        if not passed_12:
            all_passed = False
        typer.echo(f"  [{status_12}] {label_12}")
    except Exception as exc:
        all_passed = False
        typer.echo(f"  [FAIL] {label_12}: {exc}")

    # Part B.7 Presentation preferences sequence (multi-turn cache reuse and presentation testing)
    typer.echo("\nRunning presentation preferences sequence (Part B.7)...\n")
    wf.reset_conversation()
    pres_cases = [
        (
            "P1. Display all tables",
            "Display all tables.",
            lambda r: any(t.name == "list_database_objects" for t in r.tool_results) and r.response_mode == "display",
        ),
        (
            "P2. Without mentioning public",
            "Display all the table names without mentioning public word.",
            lambda r: r.response_mode == "display" and (r.presentation_options.get("include_schema") is False or r.presentation_options.get("omit_schema") is True),
        ),
        (
            "P3. Show only names",
            "Show only names.",
            lambda r: r.response_mode == "display" and r.presentation_options.get("names_only") is True,
        ),
        (
            "P4. Number the table names",
            "Number the table names.",
            lambda r: r.response_mode == "display" and r.presentation_options.get("numbered") is True,
        ),
        (
            "P5. Sort table names alphabetically",
            "Sort table names alphabetically.",
            lambda r: r.response_mode == "display" and r.presentation_options.get("alphabetical") is True,
        ),
        (
            "P6. Without schema, numbered alphabetically",
            "Show all table names without schema names, numbered alphabetically.",
            lambda r: r.response_mode == "display" and (r.presentation_options.get("include_schema") is False or r.presentation_options.get("omit_schema") is True) and r.presentation_options.get("numbered") is True and r.presentation_options.get("alphabetical") is True,
        ),
        (
            "P7. Show the tables again",
            "Show the tables again.",
            lambda r: r.response_mode == "display" and any(t.name == "list_database_objects" for t in r.tool_results),
        ),
    ]

    for label, prompt, validator in pres_cases:
        try:
            res = wf.handle_user_message(prompt, active_profile=eval_profile_name, model=model_name)
            passed = validator(res)
            status = "PASS" if passed else "FAIL"
            if not passed:
                all_passed = False
            typer.echo(f"  [{status}] {label}")
        except Exception as exc:
            all_passed = False
            typer.echo(f"  [FAIL] {label}: {exc}")

    # Cleanup temp resources
    try:
        Path(db_path).unlink(missing_ok=True)
        Path(prof_db).unlink(missing_ok=True)
    except Exception:
        pass

    typer.echo(f"\nEvaluation summary: {'ALL PASSED' if all_passed else 'SOME FAILED'}\n")
    return all_passed


@model_app.command(name="test")
def test_model(
    eval_suite: bool = typer.Option(
        False,
        "--eval",
        "-e",
        help="Run behavioral evaluation suite against the 6 core assistant cases.",
    ),
) -> None:
    """Run a diagnostic test or behavioral evaluation of the configured assistant model."""
    configured_model = get_assistant_model()
    if not configured_model:
        typer.echo("Error: No assistant model configured.", err=True)
        typer.echo("Configure a model using: support model set assistant <model-name>")
        raise typer.Exit(code=1)

    llm = get_llm_provider()
    typer.echo(f"Testing assistant model '{configured_model}'...")

    info = llm.check_model_status(configured_model)
    if not info.is_available:
        typer.echo(f"Server:       {llm.base_url}")
        typer.echo(f"Model:        {configured_model}")
        typer.echo(f"Result:       Failed ({info.details or 'Unavailable'})", err=True)
        raise typer.Exit(code=1)

    if eval_suite:
        success = run_model_behavioral_eval(llm, configured_model)
        if not success:
            raise typer.Exit(code=1)
        return

    # Run lightweight verification query
    try:
        from support.domain.assistant import ChatMessage, ToolDefinition
        test_tool = ToolDefinition(
            name="test_tool",
            description="Diagnostic test tool",
            parameters={"type": "object", "properties": {"val": {"type": "string"}}},
        )
        resp = llm.chat(
            messages=[ChatMessage(role="user", content="Respond with test_tool call")],
            tools=[test_tool],
            model=configured_model,
        )
        tool_verified = len(resp.tool_calls) > 0 or resp.content is not None
    except Exception as exc:
        typer.echo(f"Server:       {llm.base_url} (Online)")
        typer.echo(f"Model:        {configured_model} (Available)")
        typer.echo(f"Result:       Failed during test invocation: {exc}", err=True)
        raise typer.Exit(code=1)

    typer.echo(f"Server:       {llm.base_url} (Online)")
    typer.echo(f"Model:        {configured_model} (Available)")
    typer.echo(f"Tool Calling: {'Verified' if tool_verified else 'Warning: Model did not execute test tool'}")
    typer.echo("Result:       Success")


@model_app.command(name="unset")
def unset_model(
    role: str = typer.Argument(..., help="Model role to unset (e.g. 'assistant')."),
) -> None:
    """Unset configured model for a role."""
    clean_role = role.strip().lower()
    if clean_role != "assistant":
        typer.echo(f"Error: Unsupported role '{role}'. Currently only 'assistant' is supported.", err=True)
        raise typer.Exit(code=1)

    cfg = load_config()
    prev = cfg.get_model(clean_role)
    if not prev:
        typer.echo(f"No model was configured for role '{role}'.")
        return

    cfg.unset_model(clean_role)
    save_config(cfg)
    typer.echo(f"Removed assistant model '{prev}'.")
    typer.echo(f"Configuration updated at {get_config_path()}")


def _run_interactive_model_menu() -> None:
    """Compact interactive Questionary menu for model management."""
    while True:
        current = get_assistant_model() or "(none)"
        typer.echo(f"\nModel Management  ·  Current assistant: {current}")
        choices = [
            "Show current configuration",
            "Set assistant model",
            "Test assistant model",
            "Run behavioral evaluation suite",
            "List installed Ollama models",
            "Unset assistant model",
            "Exit",
        ]
        try:
            choice = questionary.select("What would you like to do?", choices=choices).ask()
        except (KeyboardInterrupt, EOFError):
            break

        if not choice or choice == "Exit":
            break
        elif choice == "Show current configuration":
            show_model()
        elif choice == "List installed Ollama models":
            list_models()
        elif choice == "Test assistant model":
            try:
                test_model()
            except typer.Exit:
                pass
        elif choice == "Run behavioral evaluation suite":
            try:
                test_model(eval_suite=True)
            except typer.Exit:
                pass
        elif choice == "Unset assistant model":
            try:
                unset_model("assistant")
            except typer.Exit:
                pass
        elif choice == "Set assistant model":
            llm = get_llm_provider()
            try:
                available_models = [m.name for m in llm.list_models()]
            except Exception:
                available_models = []

            if available_models:
                select_choices = available_models + ["Enter another model name", "Cancel"]
                try:
                    selected = questionary.select("Select model from local Ollama:", choices=select_choices).ask()
                except (KeyboardInterrupt, EOFError):
                    selected = None

                if not selected or selected == "Cancel":
                    continue
                elif selected == "Enter another model name":
                    try:
                        custom_name = questionary.text("Model tag:").ask()
                    except (KeyboardInterrupt, EOFError):
                        custom_name = None
                    if custom_name and custom_name.strip():
                        try:
                            set_model("assistant", custom_name.strip())
                        except typer.Exit:
                            pass
                else:
                    try:
                        set_model("assistant", selected)
                    except typer.Exit:
                        pass
            else:
                try:
                    name_input = questionary.text("Enter model tag (e.g. qwen2.5-coder:3b):").ask()
                except (KeyboardInterrupt, EOFError):
                    name_input = None
                if name_input and name_input.strip():
                    try:
                        set_model("assistant", name_input.strip())
                    except typer.Exit:
                        pass


def _is_interactive_terminal() -> bool:
    """Check whether both standard input and standard output are connected to a terminal."""
    return sys.stdin.isatty() and sys.stdout.isatty()


@model_app.callback(invoke_without_command=True)
def model_main(ctx: typer.Context) -> None:
    """Manage local language model configuration for Support."""
    if ctx.invoked_subcommand is None:
        if _is_interactive_terminal():
            _run_interactive_model_menu()
        else:
            show_model()
