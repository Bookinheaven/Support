"""CLI commands for managing LLM providers and models."""

from typing import Optional
import typer

from rich.console import Console

from support.cli.formatters import (
    print_failure,
    print_step,
    print_success,
    print_warning,
    render_current_model_panel,
    render_models_table,
)
from support.core.exceptions import LLMError, SupportError
from support.llm.models import GenerationSettings, LLMProviderType, ProviderStatus
from support.llm.service import LLMService

console = Console()

models_app = typer.Typer(
    name="models",
    help="Manage LLM providers (Ollama, Grok, Gemini) and models.",
    invoke_without_command=True,
    add_completion=False,
)


def _list_models_impl(service: LLMService, provider: Optional[str] = None) -> None:
    """Core logic for listing models."""
    config = service.get_config()
    with console.status("[cyan]Discovering models and validating providers...[/cyan]", spinner="dots"):
        models_by_provider = service.list_models(provider=provider)
        validations = service.validate_all() if not provider else {
            provider: service.validate_provider(provider)
        }

    render_models_table(
        models_by_provider=models_by_provider,
        validation_results=validations,
        active_provider=config.default_provider,
        active_model=config.default_model,
    )


@models_app.callback(invoke_without_command=True)
def models_callback(
    ctx: typer.Context,
    provider: Optional[str] = typer.Option(
        None,
        "--provider",
        "-p",
        help="Filter models by provider (ollama, grok, gemini).",
    ),
) -> None:
    """Manage LLM providers and models. Defaults to listing models."""
    if ctx.invoked_subcommand is None:
        service = LLMService()
        _list_models_impl(service, provider=provider)


@models_app.command("list")
def models_list(
    provider: Optional[str] = typer.Option(
        None,
        "--provider",
        "-p",
        help="Filter models by provider (ollama, grok, gemini).",
    ),
) -> None:
    """List available and configured models across all providers."""
    service = LLMService()
    _list_models_impl(service, provider=provider)


@models_app.command("current")
def models_current() -> None:
    """Display the currently active LLM provider, model, and status."""
    service = LLMService()
    config = service.get_config()
    validation = service.validate_provider(
        provider=config.default_provider, model=config.default_model
    )

    render_current_model_panel(
        provider=config.default_provider,
        model=config.default_model,
        validation=validation,
        config=config,
    )


@models_app.command("use")
def models_use(
    provider: str = typer.Argument(
        ...,
        help="Provider to activate (ollama, grok, gemini).",
    ),
    model: Optional[str] = typer.Option(
        None,
        "--model",
        "-m",
        help="Specific model ID to use with this provider.",
    ),
) -> None:
    """Select the active LLM provider and model."""
    service = LLMService()
    try:
        prov_enum = LLMProviderType.from_string(provider)
    except ValueError as e:
        print_failure(str(e))
        raise typer.Exit(1)

    config = service.get_config()
    target_model = model or config.get_model_for_provider(prov_enum.value)

    # Validate provider and model
    validation = service.validate_provider(prov_enum.value, model=target_model)

    if validation.status == ProviderStatus.NOT_CONFIGURED:
        print_warning(
            f"Provider '{prov_enum.value}' is selected, but not configured."
        )
        if validation.hint:
            typer.echo(f"  {validation.hint}")
    elif validation.status == ProviderStatus.MODEL_UNAVAILABLE:
        print_warning(
            f"Model '{target_model}' is not currently available on '{prov_enum.value}'."
        )
        if validation.hint:
            typer.echo(f"  {validation.hint}")
    elif validation.status == ProviderStatus.UNREACHABLE:
        print_warning(f"Provider '{prov_enum.value}' is currently unreachable.")
        if validation.hint:
            typer.echo(f"  {validation.hint}")
    elif validation.status == ProviderStatus.AUTH_FAILED:
        print_warning(f"Authentication failed for '{prov_enum.value}'.")
        if validation.hint:
            typer.echo(f"  {validation.hint}")

    # Persist the configuration change
    service.set_active_provider(prov_enum.value, model=target_model)
    print_success(
        f"Active model set to '{target_model}' ({prov_enum.value})."
    )


@models_app.command("test")
def models_test(
    provider: Optional[str] = typer.Argument(
        None,
        help="Optional provider to test (ollama, grok, gemini). Defaults to active provider.",
    ),
    model: Optional[str] = typer.Option(
        None,
        "--model",
        "-m",
        help="Optional model ID to test. Defaults to active or provider default.",
    ),
    prompt: str = typer.Option(
        "Reply with 'LLM Gateway is operational' and nothing else.",
        "--prompt",
        help="Test prompt to send.",
    ),
) -> None:
    """Test connectivity and generation with an LLM provider."""
    service = LLMService()
    config = service.get_config()

    target_provider = provider or config.default_provider
    try:
        prov_enum = LLMProviderType.from_string(target_provider)
    except ValueError as e:
        print_failure(str(e))
        raise typer.Exit(1)

    target_model = model or (
        config.default_model
        if target_provider == config.default_provider
        else config.get_model_for_provider(prov_enum.value)
    )

    try:
        with console.status(f"[cyan]Testing {prov_enum.value} with model '{target_model}'...[/cyan]", spinner="dots"):
            response = service.generate(
                prompt=prompt,
                provider=prov_enum.value,
                model=target_model,
                settings=GenerationSettings(
                    temperature=0.0,
                    timeout=config.timeout,
                    max_tokens=50,
                ),
            )

        lat_str = (
            f" · {response.latency_ms:.0f} ms"
            if response.latency_ms is not None
            else ""
        )
        tokens_str = (
            f" · {response.usage.display_summary}"
            if response.usage and response.usage.display_summary != "-"
            else ""
        )

        print_success(
            f"Response received from {response.provider}/{response.model}{lat_str}{tokens_str}"
        )
        # Display response snippet indented
        clean_text = response.text.strip().replace("\n", " ")
        typer.echo(f"  \"{clean_text}\"")

    except SupportError as e:
        print_failure(e.message, details=e.details)
        raise typer.Exit(1)
    except Exception as e:
        print_failure(f"Unexpected generation error: {e}")
        raise typer.Exit(1)
