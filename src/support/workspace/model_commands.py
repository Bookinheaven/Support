"""LLM provider and model selection handlers for Support workspace."""

from typing import Optional
from rich import box
from rich.table import Table

from support.cli.display import console
from support.llm.service import LLMService
from support.workspace.state import WorkspaceSessionState


def handle_model_selection(
    state: WorkspaceSessionState,
    llm_service: LLMService,
    user_input_func=input,
    checker: Optional[object] = None,
) -> None:
    """Handle /model command: display available providers and models and let user select one for this session."""
    console.print()
    console.print("[bold cyan]LLM Providers and Models[/bold cyan]")

    with console.status("[cyan]Discovering models and validating providers...[/cyan]", spinner="dots"):
        all_models = llm_service.list_models()
        val_results = llm_service.validate_all()

    # Build flattened options list
    options: list[tuple[str, str, str, str]] = []  # (provider, model_id, status_str, details)

    table = Table(
        box=box.SIMPLE,
        header_style="bold cyan",
        show_header=True,
        pad_edge=False,
    )
    table.add_column("#", justify="right", style="dim", no_wrap=True)
    table.add_column("Provider", style="bold white", no_wrap=True)
    table.add_column("Model ID", style="cyan", no_wrap=True)
    table.add_column("Status", justify="center", no_wrap=True)
    table.add_column("Details", style="dim white")

    opt_idx = 1
    for prov_name, models in all_models.items():
        v_res = val_results.get(prov_name)
        status_enum = getattr(v_res, "status", None)
        status_str = status_enum.value if status_enum else "UNKNOWN"

        if status_str == "AVAILABLE":
            status_cell = "[bold green]AVAILABLE[/bold green]"
        elif status_str == "NOT CONFIGURED":
            status_cell = "[bold yellow]NOT CONFIGURED[/bold yellow]"
        elif status_str == "UNREACHABLE":
            status_cell = "[bold red]UNREACHABLE[/bold red]"
        else:
            status_cell = f"[bold red]{status_str}[/bold red]"

        for m in models:
            is_active = (
                prov_name == state.active_provider and m.id == state.active_model
            )
            prefix = "[bold green]*[/bold green] " if is_active else "  "
            model_cell = f"{prefix}{m.id}"
            details = m.description or (f"{m.size_bytes // 1_000_000} MB" if m.size_bytes else "")

            table.add_row(
                str(opt_idx),
                prov_name,
                model_cell,
                status_cell,
                details,
            )
            options.append((prov_name, m.id, status_str, details))
            opt_idx += 1

    console.print(table)
    console.print("[dim]* Active session model[/dim]")
    console.print()

    # Prompt user for selection
    try:
        choice = user_input_func(
            f"Select model [1-{len(options)}, or Enter to keep '{state.active_provider}/{state.active_model}']: "
        ).strip()
    except (KeyboardInterrupt, EOFError):
        console.print("\n[dim]Selection cancelled.[/dim]")
        return

    if not choice:
        console.print("[dim]Model selection unchanged.[/dim]")
        return

    chosen_option = None
    if choice.isdigit():
        idx = int(choice)
        if 1 <= idx <= len(options):
            chosen_option = options[idx - 1]
    else:
        # Match by model id or provider/model string
        clean_choice = choice.lower()
        for opt in options:
            p_name, m_id, _, _ = opt
            combined = f"{p_name}/{m_id}".lower()
            if m_id.lower() == clean_choice or combined == clean_choice:
                chosen_option = opt
                break

    if not chosen_option:
        console.print(f"[bold red]Error:[/bold red] Unknown model option '{choice}'.")
        return

    prov_chosen, model_chosen, stat_chosen, _ = chosen_option

    # Validate connectivity for the chosen model
    try:
        val = llm_service.validate_provider(prov_chosen, model_chosen)
        stat_text = val.status.value
        if val.latency_ms is not None:
            stat_text += f" ({val.latency_ms:.0f} ms)"
    except Exception as e:
        stat_text = f"Unverified: {e}"

    if "NOT CONFIGURED" in stat_text:
        console.print(
            f"[yellow]Notice:[/yellow] Provider '{prov_chosen}' is not configured. "
            f"Run 'support auth login {prov_chosen}' to set credentials."
        )
    elif "UNREACHABLE" in stat_text:
        console.print(
            f"[yellow]Notice:[/yellow] Provider '{prov_chosen}' could not be reached. "
            f"Ensure the service is running or check network connectivity."
        )

    state.set_model(prov_chosen, model_chosen, stat_text)
    if checker and hasattr(checker, "check_model"):
        checker.check_model()

    console.print(
        f"[bold green]✓[/bold green] Active model set to [bold white]{prov_chosen}/{model_chosen}[/bold white] "
        f"[dim](session-scoped)[/dim]."
    )
