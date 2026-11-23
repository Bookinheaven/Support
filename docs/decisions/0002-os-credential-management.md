# 2. OS-Native Credential Storage and Backend Allowlist Policy

## Context

Database connection profiles in Support require credentials (passwords) for future database operations.
Storing passwords in SQLite alongside profile metadata introduces security risks (e.g. database files copied, committed, or shared in backups).
We need a cross-platform mechanism to securely store, retrieve, and delete passwords while protecting sensitive user data.

## Decision

We use the Python `keyring` library to delegate secret management exclusively to operating-system native credential stores:

1. **Storage Mechanism & Identity**:
   - Secrets are keyed by a constant service name (`support.database`) and the profile's internal immutable `UUID`.
   - Human-readable profile names are never used as credential keys because profile renaming must not orphan or desynchronize credentials.
   - Profile models (`DatabaseProfile`, `PostgresConnection`) and SQLite tables (`profiles`) remain completely free of secrets and secret tokens.

2. **Strict Backend Allowlist Policy**:
   - Support rejects automatic selection of arbitrary keyring plugins or fallback backends.
   - Only native OS-integrated secure backends are approved:
     - **Windows**: Windows Credential Locker (`keyring.backends.Windows.WinVaultKeyring`).
     - **macOS**: Apple Keychain (`keyring.backends.macOS.Keyring`).
     - **Linux**: FreeDesktop Secret Service (`keyring.backends.SecretService.Keyring`) and KDE Wallet (`keyring.backends.kwallet.DBusKeyring` / `DBusKeyringKWallet4`).
   - All null, fail, plaintext file, encrypted file, alternative, or unknown third-party backends are rejected with `CredentialStoreUnavailableError`.

3. **Rejection of Plaintext / File Fallbacks**:
   - When a native secure credential store is unavailable (such as headless servers without a D-Bus secret service), Support refuses to store passwords.
   - Insecure fallbacks (plaintext files, `.env` files, or storing secrets in SQLite) are strictly prohibited. In headless environments, users will supply one-time passwords interactively at connection time.

4. **Cross-Resource Deletion Lifecycle**:
   - Because SQLite and OS keyrings cannot participate in a single atomic transaction, profile deletion follows a deliberate 5-step sequence:
     1. Resolve and verify the profile exists in SQLite.
     2. Attempt to delete its saved credential from the native store.
     3. If credential deletion fails, abort the operation immediately and preserve the profile in SQLite.
     4. Delete the profile record from SQLite after credential deletion succeeds.
     5. If SQLite deletion fails after credential removal, report clearly that the profile record remains but its password was removed.

## Consequences

- Passwords receive hardware-backed or OS-level encryption (DPAPI on Windows, Keychain Secure Enclave on macOS, Secret Service master password on Linux).
- Profile renames seamlessly retain their password bindings without touching the credential store.
- Testing is decoupled from real OS keyrings via deterministic in-memory backends.
