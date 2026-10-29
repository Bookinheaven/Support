"""CLI commands for managing secure LLM cloud provider authentication."""

import os
from typing import Optional
import typer

from support.cli.display import console
from support.cli.formatters import render_auth_status_table
from support.core.exceptions import KeyringUnavailableError
from support.llm.credentials import (
    CredentialManager,
    PROVIDER_CREDENTIALS,
)
from support.llm.models import ProviderStatus
from support.llm.providers.gemini import GeminiProvider
from support.llm.providers.grok import GrokProvider

auth_app = typer.Typer(
    name="auth",
    help="Manage secure LLM cloud provider credentials and authentication.",
    no_args_is_help=True,
)


def _get_provider_instance(provider_name: str, api_key: Optional[str] = None):
    """Instantiate provider adapter for testing or validation."""
    prov = provider_name.lower()
    if prov == "grok":
        return GrokProvider(api_key=api_key)
    elif prov == "gemini":
        return GeminiProvider(api_key=api_key)
    return None


@auth_app.command("status")
def auth_status() -> None:
    """Show authentication and credential status for all cloud providers."""
    rows = []
    for prov_key, cfg in PROVIDER_CREDENTIALS.items():
        is_configured, source = CredentialManager.get_credential_source(prov_key)
        status_str = "CONFIGURED" if is_configured else "NOT CONFIGURED"
        details = (
            cfg["display_name"]
            if is_configured
            else f"Run 'support auth login {prov_key}'"
        )
        rows.append(
            {
                "provider": cfg["display_name"],
                "status": status_str,
                "source": source,
                "details": details,
            }
        )

    render_auth_status_table(rows)


@auth_app.command("login")
def auth_login(
    provider: str = typer.Argument(
        ...,
        help="Cloud provider to authenticate (grok, gemini).",
    ),
) -> None:
    """Prompt for API key, validate it against the provider API, and save to OS secure keyring."""
    prov_key = provider.lower()
    if prov_key == "ollama":
        console.print("[yellow]Ollama is a local provider and does not require an API key.[/yellow]")
        console.print("Configure Ollama with: [bold]support models use ollama[/bold]")
        return

    cfg = PROVIDER_CREDENTIALS.get(prov_key)
    if not cfg:
        console.print(
            f"[bold red]Error:[/bold red] Unsupported provider '{provider}'. "
            f"Supported cloud providers: [bold]{', '.join(PROVIDER_CREDENTIALS.keys())}[/bold]"
        )
        raise typer.Exit(code=1)

    display_name = cfg["display_name"]

    # Secure prompt with hidden terminal input
    api_key = typer.prompt(
        f"Enter {display_name} API key",
        hide_input=True,
    ).strip()

    if not api_key:
        console.print("[bold red]Error:[/bold red] API key cannot be empty.")
        raise typer.Exit(code=1)

    # Validate key against provider API prior to saving
    console.print(f"Validating {display_name} credentials...")
    provider_inst = _get_provider_instance(prov_key, api_key=api_key)
    if not provider_inst:
        console.print(f"[bold red]Error:[/bold red] Unable to instantiate adapter for '{prov_key}'.")
        raise typer.Exit(code=1)

    val_res = provider_inst.validate_configuration()

    if val_res.status in (ProviderStatus.AVAILABLE, ProviderStatus.MODEL_UNAVAILABLE):
        try:
            CredentialManager.set_api_key(prov_key, api_key)
            backend_name = CredentialManager.get_backend_name()
            console.print(
                f"[bold green]✓[/bold green] [bold]{display_name}[/bold] credentials validated "
                f"and securely stored in [cyan]{backend_name}[/cyan]."
            )
        except KeyringUnavailableError as e:
            console.print(f"[bold red]✗ Failed to store credentials:[/bold red] {e}")
            raise typer.Exit(code=1)
    elif val_res.status == ProviderStatus.AUTH_FAILED:
        console.print(f"[bold red]✗ Authentication failed:[/bold red] {val_res.message}")
        if val_res.hint:
            console.print(f"  [yellow]{val_res.hint}[/yellow]")
        console.print("[dim]Key was not saved.[/dim]")
        raise typer.Exit(code=1)
    elif val_res.status == ProviderStatus.UNREACHABLE:
        console.print(f"[bold red]✗ Provider unreachable:[/bold red] {val_res.message}")
        if val_res.hint:
            console.print(f"  [yellow]{val_res.hint}[/yellow]")
        console.print("[dim]Key was not saved because API connectivity could not be verified.[/dim]")
        raise typer.Exit(code=1)
    else:
        console.print(f"[bold red]✗ Validation failed:[/bold red] {val_res.message}")
        raise typer.Exit(code=1)


@auth_app.command("test")
def auth_test(
    provider: str = typer.Argument(
        ...,
        help="Cloud provider to test (grok, gemini).",
    ),
) -> None:
    """Verify stored credentials and provider API connectivity without changing active configuration."""
    prov_key = provider.lower()
    if prov_key == "ollama":
        console.print("[yellow]Ollama is a local provider. Use 'support models test ollama' instead.[/yellow]")
        return

    cfg = PROVIDER_CREDENTIALS.get(prov_key)
    if not cfg:
        console.print(
            f"[bold red]Error:[/bold red] Unsupported provider '{provider}'. "
            f"Supported cloud providers: [bold]{', '.join(PROVIDER_CREDENTIALS.keys())}[/bold]"
        )
        raise typer.Exit(code=1)

    display_name = cfg["display_name"]
    api_key = CredentialManager.get_api_key(prov_key)

    if not api_key:
        console.print(f"[bold red]✗ Provider '{prov_key}' is not configured[/bold red]")
        console.print(f"  Configure credentials with: [bold]support auth login {prov_key}[/bold] (or set {cfg['env_var']})")
        raise typer.Exit(code=1)

    with console.status(f"[cyan]Testing {display_name} authentication and connectivity...[/cyan]", spinner="dots"):
        provider_inst = _get_provider_instance(prov_key, api_key=api_key)
        val_res = provider_inst.validate_configuration()

    if val_res.status in (ProviderStatus.AVAILABLE, ProviderStatus.MODEL_UNAVAILABLE):
        lat_str = f" · {val_res.latency_ms:.0f} ms" if val_res.latency_ms is not None else ""
        console.print(
            f"[bold green]✓[/bold green] [bold]{display_name}[/bold] credentials and API connectivity verified{lat_str}."
        )
    elif val_res.status == ProviderStatus.AUTH_FAILED:
        console.print(f"[bold red]✗ Authentication failed:[/bold red] {val_res.message}")
        if val_res.hint:
            console.print(f"  [yellow]{val_res.hint}[/yellow]")
        raise typer.Exit(code=1)
    else:
        console.print(f"[bold red]✗ Provider check failed:[/bold red] {val_res.message}")
        if val_res.hint:
            console.print(f"  [yellow]{val_res.hint}[/yellow]")
        raise typer.Exit(code=1)


@auth_app.command("logout")
def auth_logout(
    provider: str = typer.Argument(
        ...,
        help="Cloud provider to remove credentials for (grok, gemini).",
    ),
    yes: bool = typer.Option(
        False,
        "--yes",
        "-y",
        help="Confirm removal without prompting.",
    ),
) -> None:
    """Remove stored credentials for a cloud provider from the OS secure keyring."""
    prov_key = provider.lower()
    cfg = PROVIDER_CREDENTIALS.get(prov_key)
    if not cfg:
        console.print(
            f"[bold red]Error:[/bold red] Unsupported provider '{provider}'. "
            f"Supported cloud providers: [bold]{', '.join(PROVIDER_CREDENTIALS.keys())}[/bold]"
        )
        raise typer.Exit(code=1)

    display_name = cfg["display_name"]
    env_var = cfg["env_var"]

    # Check if credentials exist in keyring
    keyring_has_key = False
    if CredentialManager.is_keyring_available():
        try:
            import keyring
            val = keyring.get_password(CredentialManager.SERVICE_NAME, cfg["keyring_account"])
            if val and val.strip():
                keyring_has_key = True
        except Exception:
            pass

    env_has_key = bool(os.getenv(env_var))

    if not keyring_has_key and not env_has_key:
        console.print(f"No credentials configured for '{prov_key}'.")
        return

    if keyring_has_key:
        if not yes:
            confirmed = typer.confirm(
                f"Remove stored credentials for {display_name} from OS keyring?",
                default=False,
            )
            if not confirmed:
                console.print("Operation cancelled.")
                return

        CredentialManager.delete_api_key(prov_key)
        console.print(f"[bold green]✓[/bold green] Removed stored credentials for [bold]{display_name}[/bold] from OS keyring.")
        if env_has_key:
            console.print(f"[yellow]Notice:[/yellow] {env_var} is still set in your current environment.")
    else:
        console.print(f"No credentials found in OS keyring for [bold]{display_name}[/bold].")
        if env_has_key:
            console.print(
                f"[yellow]Notice:[/yellow] {env_var} is set in your current environment. "
                f"Unset it in your shell/session to log out completely."
            )
