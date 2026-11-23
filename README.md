# Support

**Developer-First Terminal Database Workspace**

Support is an AI-assisted database intelligence and operations workspace designed for backend and full-stack developers. It helps engineers understand database structures, investigate data, generate SQL, and perform controlled database operations.

---

## Intended Users

- **Backend Engineers**: Inspecting schemas, reviewing relationships, and composing queries without leaving the terminal.
- **Full-Stack Developers**: Exploring unfamiliar databases and safely verifying data changes during development.

---

## Current Status

Support is in active incremental development (**v0.1.0**).

The current implementation provides:
- Minimal Typer-based CLI entry point with version (`--version`, `-V`) and help commands.
- Multi-database domain models for typed connection configurations:
  - **PostgreSQL** (`PostgresConnection`)
  - **MySQL** (`MySQLConnection`)
  - **MariaDB** (`MariaDBConnection`)
  - **SQLite** (`SQLiteConnection`)
  - **Microsoft SQL Server** (`SQLServerConnection`)
  - **MongoDB** (`MongoDBConnection`)
- Persistent local profile management backed by SQLite schema version 1 (`support profile`).
- Secure OS-native credential storage via Python `keyring` (`support profile password`).
- Extensible connector dispatch architecture with live connection testing:
  - **PostgreSQL** via synchronous Psycopg 3 (`support profile test`).
  - **SQLite** via standard-library `sqlite3` in read-only mode (`support profile test`).
  - **MySQL & MariaDB** via pure-Python PyMySQL with RSA auth (`support profile test`).

> **Note on Security & Scope**:
> - **SQLite schema version 1 stores serialized connection configuration only**. Passwords and secret credentials are never stored in SQLite.
> - **Passwords are stored securely in OS-native credential managers** using immutable profile UUID identities. Embedded databases (SQLite) require no passwords.
> - **Connection testing** verifies reachability and credentials through an explicit connector dispatcher (currently active for PostgreSQL, SQLite, MySQL, and MariaDB). Additional database driver adapters, schema inspection, and query generation will be introduced incrementally.
> - **Authentication Methods**: Standard username/password authentication is supported across PostgreSQL, MySQL, and MariaDB. Advanced enterprise mechanisms (such as AWS IAM RDS, GCP Cloud SQL IAM, Azure Entra ID, Kerberos/GSSAPI, LDAP, client-certificate mTLS, and MariaDB `ed25519`) are explicitly deferred.
> - **SQLite Connection Testing & Read-Only Safety**:
>   - SQLite connection testing validates the SQLite file signature (`SQLite format 3\0`), 100-byte header completeness, and encoded page size. Missing, zero-byte, and non-SQLite files are rejected with clear error messages.
>   - File-backed databases are opened in read-only mode (`mode=ro`). Support does not execute data-changing queries or modify application data.
>   - For databases operating in Write-Ahead Logging (WAL) mode, SQLite WAL operation may require or create internal `-wal` and `-shm` sidecars depending on database state and filesystem permissions. The `immutable=1` flag is intentionally not used as a universal workaround because it is unsafe on live databases with concurrent writers.
> - **Pre-Release Schema Policy**: The multi-database schema is the initial canonical schema version 1. Incompatible pre-release development databases must be backed up and recreated manually; proper migrations will be introduced when preserving released-user data becomes necessary.

---

## Profile Management

Manage database connection profiles using the `support profile` command group.

### Storage Location & Configuration
- **Default path**: Stored as `profiles.sqlite3` in the operating system's standard user configuration directory (resolved via `platformdirs`):
  - Linux: `~/.config/support/profiles.sqlite3`
  - macOS: `~/Library/Application Support/support/profiles.sqlite3`
  - Windows: `%LOCALAPPDATA%\support\profiles.sqlite3`
- **Environment Override**: Set `SUPPORT_HOME` to specify a custom root directory for `profiles.sqlite3` (e.g. `export SUPPORT_HOME=/custom/path`).
- **File Permissions**: On POSIX systems, directory permissions are set to `0700` and the database file to `0600`.

### Commands

#### 1. Add a Profile
Create a new profile with non-secret connection parameters (defaults to `--type postgresql`):
```bash
# PostgreSQL profile
support profile add --name local-dev --database app_db --username app_user --ssl-mode prefer

# SQLite profile
support profile add --type sqlite --name local-sqlite --database-path ./app.db

# MySQL profile
support profile add --type mysql --name local-mysql --database shop --username admin --ssl-mode preferred

# MariaDB profile
support profile add --type mariadb --name local-maria --database analytics --username analyst --ssl-mode prefer
```
Supported `--type` options: `postgresql`, `mysql`, `mariadb`, `sqlite`, `sqlserver`, `mongodb`.
Optional flags depending on database type:
- `--host`: Database host (default: `localhost`).
- `--port`: Database port (default: database standard port, e.g. 5432 for PostgreSQL, 3306 for MySQL/MariaDB).
- `--ssl-mode`: SSL mode:
  - PostgreSQL: `disable`, `allow`, `prefer`, `require`, `verify-ca`, `verify-full`.
  - MySQL: `disabled`, `preferred`, `required`, `verify_ca`, `verify_identity`.
  - MariaDB: `disabled`, `prefer`, `require`, `verify-ca`, `verify-full`.
- `--database-path`: File path for SQLite.

#### 2. List Profiles
List all saved profiles in alphabetical order:
```bash
support profile list
```

#### 3. Show Profile Details
View the non-secret configuration and stable UUID of a profile:
```bash
support profile show local-dev
```

#### 4. Rename a Profile
Rename a profile while preserving its unique ID and connection settings:
```bash
support profile rename local-dev dev-primary
```

#### 5. Remove a Profile
Delete a profile and its associated saved credentials:
```bash
support profile remove dev-primary
```
Or skip the confirmation prompt:
```bash
support profile remove dev-primary --yes
```

#### 6. Test a Profile Connection
Verify database connectivity, authentication, and database status:
```bash
# Test PostgreSQL profile
support profile test dev-primary

# Test SQLite profile
support profile test local-sqlite

# Test MySQL profile
support profile test local-mysql

# Test MariaDB profile
support profile test local-maria
```

- **Credential Handling**: Automatically uses a stored password if present for client-server databases (PostgreSQL, MySQL, MariaDB). If no saved password exists and the command is run interactively, securely prompts for a one-time password (hidden input, press Enter for passwordless attempt). If run non-interactively without a saved password, fails cleanly with instructions. Embedded databases (SQLite) bypass password resolution and prompts entirely.
- **Connection Lifecycle & Safety**:
  - Enforces finite timeouts (10 seconds) and executes `SELECT 1`.
  - For PostgreSQL: validates the exact SSL mode and reports server version via Psycopg 3.
  - For MySQL & MariaDB: validates TLS semantics (`disabled`, `preferred`/`prefer`, `required`/`require`, `verify_ca`/`verify-ca`, `verify_identity`/`verify-full`), uses system trust roots, disables `local_infile`, avoids loading client option files, and reports server version via PyMySQL.
  - For SQLite: validates the file signature (`SQLite format 3\0`), header completeness, and page size before opening; rejects zero-byte, truncated, or non-SQLite files; opens the main database in read-only mode (`mode=ro`); supports in-memory databases (`:memory:`); and never executes data-changing queries. (Note: WAL-mode databases may require or create internal `-wal` and `-shm` sidecars; `immutable=1` is intentionally avoided as it is unsafe for live concurrent databases).
  - Connections are deterministically closed immediately in all cases.
- **Scope**: Verifies baseline connection and authentication. It does not perform schema reflection or catalog queries.

---

## Password Management

Manage database credentials securely via OS-native credential stores using the `support profile password` command group.

### Supported Operating-System Stores
Passwords are tied to internal profile UUIDs and stored exclusively through verified native backends:
- **Windows**: Windows Credential Locker (`keyring.backends.Windows.WinVaultKeyring`)
- **macOS**: Apple Keychain (`keyring.backends.macOS.Keyring`)
- **Linux**: FreeDesktop Secret Service (`keyring.backends.SecretService.Keyring`) or KDE Wallet (`keyring.backends.kwallet.DBusKeyring`)

> **Fallback Policy**:
> Support strictly enforces an allowlist of native secure backends. Plaintext files, `.env` files, encrypted file stores, and unverified third-party backends are rejected. If a secure OS store is unavailable (e.g. in headless environments), Support will not persist passwords to disk.

### Commands

#### 1. Set a Profile Password
Prompt for a password with hidden terminal input and confirmation:
```bash
support profile password set dev-primary
```
*(Passwords are never accepted as command-line arguments to prevent leakage into shell history and process tables).*

#### 2. Check Password Status
Check whether a password has been stored for a profile without displaying the secret:
```bash
support profile password status dev-primary
```

#### 3. Remove a Profile Password
Remove the stored password for a profile from the OS credential store:
```bash
support profile password remove dev-primary
```

---

## Development Setup

### 1. Prerequisites
- **Python**: Version 3.11 or higher (`python3 --version`).

### 2. Create and Activate Virtual Environment
```bash
python3 -m venv .venv
source .venv/bin/activate
```

### 3. Install in Editable Mode
Install the package in editable mode with development dependencies:
```bash
pip install -e ".[dev]"
```

---

## Usage

### Run Support
```bash
# Display welcome message
support

# View version information
support --version
support -V

# View available options and commands
support --help
```

---

## Running Tests

Run the test suite using pytest:
```bash
pytest -v
```
