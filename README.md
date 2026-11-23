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
- **Local AI-Powered Conversational Assistant**:
  - Interactive terminal REPL with natural-language database intelligence powered by Ollama.
  - Native tool-calling integration supporting Qwen 2.5 Coder (`qwen2.5-coder:3b`, `qwen2.5-coder:7b`, etc.).
  - Deterministic bounded tool cycle executing read-only database application workflows.
  - Rich-formatted deterministic outputs combined with contextual conversational explanations.
  - In-memory multi-turn session context with session reset (`clear`).
- Minimal Typer-based CLI entry point with version (`--version`, `-V`), `support db`, and `support model`.
- Interactive database workspace with compact status line and active connection selector.
- Read-only database schema intelligence across all 6 supported engines:
  - `/tables`: lists tables, views, or MongoDB collections.
  - `/schema`: high-level database overview with counts of tables, views, relationships, and indexes.
  - `/describe <object>`: detailed column definitions, PKs, UKs, FKs, check constraints, and indexes.
  - `/relationships`: foreign-key dependency mapping across relational tables.
- Native local model management (`support model list`, `set`, `show`, `test`, `unset`) with atomic persistence.
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
  - **Microsoft SQL Server** via official `mssql-python` with bundled DDBC driver (`support profile test`).
  - **MongoDB** via official `pymongo` driver (`support profile test`).

> **Note on Security & Scope**:
> - **SQLite schema version 1 stores serialized connection configuration only**. Passwords and secret credentials are never stored in SQLite.
> - **Passwords are stored securely in OS-native credential managers** using immutable profile UUID identities. Embedded databases (SQLite) require no passwords.
> - **Connection testing** verifies reachability and credentials through an explicit connector dispatcher (currently active for PostgreSQL, SQLite, MySQL, MariaDB, SQL Server, and MongoDB). Additional database driver adapters, schema inspection, and query generation will be introduced incrementally.
> - **Authentication Methods**: Standard username/password authentication is supported across PostgreSQL, MySQL, MariaDB, SQL Server, and MongoDB. Advanced enterprise mechanisms (such as AWS IAM RDS, GCP Cloud SQL IAM, Microsoft Entra ID / Azure AD, Windows Integrated Authentication / Kerberos, LDAP, client-certificate mTLS, and MariaDB `ed25519`) are explicitly deferred.
> - **SQLite Connection Testing & Read-Only Safety**:
>   - SQLite connection testing validates the SQLite file signature (`SQLite format 3\0`), 100-byte header completeness, and encoded page size. Missing, zero-byte, and non-SQLite files are rejected with clear error messages.
>   - File-backed databases are opened in read-only mode (`mode=ro`). Support does not execute data-changing queries or modify application data.
>   - For databases operating in Write-Ahead Logging (WAL) mode, SQLite WAL operation may require or create internal `-wal` and `-shm` sidecars depending on database state and filesystem permissions. The `immutable=1` flag is intentionally not used as a universal workaround because it is unsafe on live databases with concurrent writers.
> - **Pre-Release Schema Policy**: The multi-database schema is the initial canonical schema version 1. Incompatible pre-release development databases must be backed up and recreated manually; proper migrations will be introduced when preserving released-user data becomes necessary.

---

## Interactive Workspace

Support serves both command-oriented developers and developers who prefer an interactive, guided workflow.

### Launching the Workspace

When invoked without arguments in an interactive terminal:

```bash
support
```

Support opens the interactive database workspace prompt:

```text
Support

Database:  postgres-support (PostgreSQL)  ·  Online  ·  113 ms
Model:     qwen2.5-coder:3b  ·  Available

support > 
```

#### Natural Language Conversational Interaction

Support's prompt `support >` is conversational. You do not need to memorize exact command syntax. Simply ask what you want to know:

```text
support > Show all tables in this database.

Tables in app_prod:

  users
  orders
  order_items
  products

Views in app_prod:

  active_users

4 tables · 1 view

Here are the 4 tables and 1 view currently defined in app_prod.

support > Describe the orders table.

Table:       public.orders
Engine:      PostgreSQL

Columns:
  id          integer       NOT NULL  PK  DEFAULT nextval('orders_id_seq'::regclass)
  user_id     integer       NOT NULL      
  total_cents integer       NOT NULL      DEFAULT 0
  created_at  timestamp     NOT NULL      DEFAULT now()

Primary Key: (orders_pkey)
  (id)

Foreign Keys:
  public.orders.user_id -> public.users.id ON DELETE CASCADE

Indexes:
  orders_pkey (unique, BTREE)
    (id)
  idx_orders_user_id (BTREE)
    (user_id)

The orders table tracks customer orders with a composite link to users via user_id.

support > Which tables reference users?

Foreign-key relationships in app_prod:

  orders.user_id
      -> users.id (ON DELETE CASCADE)

The orders table directly references users(id) with an ON DELETE CASCADE policy.

support > Explain the relationship between orders and order_items.

Foreign-key relationships in app_prod:

  order_items.order_id
      -> orders.id (ON DELETE CASCADE)

order_items maintains a foreign key referencing orders(id). When an order is deleted, all associated line items are automatically removed in cascade.
```

#### Workspace Control Commands (Slash Commands)

The workspace strictly routes commands: **all non-empty input without a leading `/` is sent to the AI assistant.**

Commands to inspect database objects or control the workspace must be prefixed with `/`:
- `/help`: Displays the Support Workspace Guide.
- `/status`: Displays current database connectivity, server versions, and Ollama model status.
- `/clear`: Clears the screen and resets conversational session memory.
- `/tables`: Lists tables, views, or MongoDB collections.
- `/schema`: Displays high-level database schema metrics and counts.
- `/describe <object>`: Inspects detailed column definitions, constraints, and indexes.
- `/relationships`: Shows declared foreign-key relationships.
- `/exit`, `/quit`, `/q`: Exits the workspace.

Ordinary words (e.g. `tables`, `schema`, `help`, `describe users`) are treated as natural-language instructions and sent directly to Ollama. Any unrecognized slash command (e.g. `/unknown`) is rejected locally with guidance to run `/help` and is never sent to the LLM.

---

## Local Ollama AI Assistant Setup

Support is designed to work with your local AI infrastructure, ensuring complete data privacy and zero cloud dependencies.

### 1. Prerequisites
Install and start Ollama on your system:

```bash
# Install Ollama (Linux / macOS)
curl -fsSL https://ollama.com/install.sh | sh

# Start Ollama service (if not running as a system daemon)
ollama serve
```

### 2. Pull a Tool-Calling Model
Support requires a model that natively supports function calling / tool calling. We recommend **Qwen 2.5 Coder**:

```bash
# Recommended lightweight model (3.1B parameters, fast and accurate tool calling)
ollama pull qwen2.5-coder:3b

# Or larger variants for deeper reasoning
ollama pull qwen2.5-coder:7b
ollama pull qwen2.5-coder:14b
```

Verify installed models:
```bash
ollama list
```

### 3. Native Model Configuration (`support model`)

Support manages model configuration using `support model` subcommands with atomic persistence in user-level configuration (`config.json`), without requiring shell environment variable exports.

```bash
# Interactive selection menu (in TTY)
support model

# List locally installed models on your Ollama server
support model list

# Set the active assistant model (verifies reachability and local installation)
support model set assistant qwen2.5-coder:3b

# Show current model configuration and diagnostic status
support model show

# Run a diagnostic test verifying the model and tool execution
support model test

# Unset the configured model
support model unset assistant
```

When you launch `support`, the status line indicates model readiness:
- `Model: qwen2.5-coder:3b · Available`: Ollama server is reachable and model is installed locally.
- `Model: qwen2.5-coder:3b · Ollama server offline`: Ollama daemon is not running.
- `Model: qwen2.5-coder:3b · Not installed`: Model tag was not found in `ollama list`.
- `Model: Not configured`: No model configured; configure with `support model set assistant <model>`.

> **Environment Overrides**: `SUPPORT_MODEL` and `SUPPORT_OLLAMA_HOST` (or `OLLAMA_MODEL` and `OLLAMA_HOST`) can be set as optional runtime environment overrides when needed, but `support model` is the standard configuration interface.

### 4. Security Boundaries & Local-Only Architecture
- **Strictly Local**: Support communicates exclusively with your configured local Ollama endpoint. It never sends queries, database metadata, or schemas to external cloud AI APIs.
- **Read-Only Metadata Tools**: The assistant has access only to explicit read-only metadata tools (`list_database_objects`, `get_database_overview`, `describe_object`, `get_relationships`, `get_connection_status`).
- **No Arbitrary Execution**: The assistant has **zero access** to arbitrary SQL execution, data modification, arbitrary Python execution, filesystem modifications, or shell commands.
- **Zero Secrets in LLM Context**: Passwords, connection strings, URIs, and credentials are cryptographically protected in OS keyrings and are **never** included in system prompts, tool results, conversation history, or logs.
- **Deterministic Truth**: Database schemas are rendered using deterministic Python/Rich presentation directly from database catalogs. The language model explains and interprets, but never invents structural facts.

---

## Database Schema Intelligence

Support provides read-only schema introspection to help engineers explore and understand connected databases directly from the terminal.

### Workspace Commands & Examples

#### 1. Listing Database Objects (`/tables`)
Lists all user tables and views (or collections in MongoDB):

```text
support > /tables

Tables in app_prod:

  users
  orders
  order_items
  products

Views in app_prod:

  active_users

4 tables · 1 view
```

For MongoDB databases:

```text
support > /tables

Collections in analytics:

  events
  users
  sessions

3 collections
```

#### 2. High-Level Schema Overview (`/schema`)
Summarizes the overall database structural metrics:

```text
support > /schema

Database:      app_prod
Engine:        PostgreSQL
Schemas:       2
Tables:        4
Views:         1
Relationships: 3
Indexes:       6
```

For MongoDB:

```text
support > /schema

Database:    analytics
Engine:      MongoDB
Collections: 3
Indexes:     5
```

#### 3. Inspecting Object Structure (`/describe <object>`)
Provides a complete structural breakdown including columns, primary keys, foreign keys, constraints, and indexes:

```text
support > /describe orders

Table:       public.orders
Engine:      PostgreSQL

Columns:
  id          integer       NOT NULL  PK  DEFAULT nextval('orders_id_seq'::regclass)
  user_id     integer       NOT NULL      
  total_cents integer       NOT NULL      DEFAULT 0
  created_at  timestamp     NOT NULL      DEFAULT now()

Primary Key: (orders_pkey)
  (id)

Foreign Keys:
  public.orders.user_id -> public.users.id ON DELETE CASCADE

Indexes:
  orders_pkey (unique, BTREE)
    (id)
  idx_orders_user_id (BTREE)
    (user_id)
```

For MongoDB collections:

```text
support > /describe events

Collection:  events
Engine:      MongoDB

Options:
  capped: True
  size: 10485760

Validator:
  {
    "$jsonSchema": {
      "bsonType": "object",
      "required": ["event_type", "timestamp"]
    }
  }

Indexes:
  _id_ (unique)
    _id
  ts_1
    timestamp
```

#### 4. Foreign-Key Relationships (`/relationships`)
Maps relational dependencies and composite foreign keys across tables:

```text
support > /relationships

Foreign-key relationships in app_prod:

  orders.user_id
      -> users.id (ON DELETE CASCADE)

  order_items.order_id
      -> orders.id (ON DELETE CASCADE)

  order_items.product_id
      -> products.id (ON DELETE RESTRICT)

3 relationships
```

### Safety and Privacy Guarantees
- **Strictly Read-Only**: Support never modifies database schemas or table rows during introspection. File-based databases (SQLite) are opened with SQLite URI `mode=ro`.
- **System Objects Excluded**: Internal tables, metadata schemas, and system objects (`information_schema`, `pg_catalog`, `pg_toast`, `sys`, `sqlite_%`, `mysql`) are excluded by default.
- **MongoDB Safety**: Introspection uses only collection specifications (`list_collections()`) and index metadata (`index_information()`). Support **never samples documents** or executes document queries (`find()` / `find_one()`).
- **Credential Protection**: Passwords, connection URIs, and credentials are never stored in schema metadata, displayed in schema outputs, or exposed to terminal logs.

---

## Database Connection Management (`support db`)

The `support db` command group is dedicated to managing database connections and credentials.

### Interactive Management Menu

Running `support db` alone in an interactive terminal opens a compact Questionary menu:

```text
? Database Management: (Use arrow keys)
 ❯ Add connection
   List connections
   Test connection
   Remove connection
   Exit
```

- **Add connection**: Guides configuration for PostgreSQL, MySQL, MariaDB, SQLite, SQL Server, and MongoDB. **Tests connectivity before permanently saving** to ensure no broken or orphaned profiles are left behind.
- **List connections**: Displays formatted summaries of all saved connections.
- **Test connection**: Selects and tests connectivity for any saved profile.
- **Remove connection**: Selects and cleanly removes profile metadata and OS-keyring passwords with confirmation.

### Explicit CLI Subcommands

Every action can also be invoked directly with explicit commands and flags:

```bash
# Add a new connection
support db add --name prod-pg --type postgresql --database app_prod --host db.internal

# List configured connections
support db list

# Test connection reachability and credentials
support db test prod-pg

# Show detailed connection parameters
support db show prod-pg

# Remove a connection and its credentials
support db remove prod-pg --yes
```

### Non-Interactive & CI Execution

When invoked non-interactively (e.g. in automated scripts, pipes, or CI pipelines), or with `--help`, `support db` displays practical examples without prompting or hanging:

```bash
$ support db
Manage registered database connections.

Examples:
  support db add
  support db list
  support db test production
  support db remove staging

Run 'support db --help' for options.
```

### Backward Compatibility

The legacy `support profile ...` command group remains fully supported as a drop-in alias for all connection and password operations.

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

# Microsoft SQL Server profile
support profile add --type sqlserver --name local-mssql --database master --username sa --encrypt --trust-server-certificate

# MongoDB profile (standard URI)
support profile add --type mongodb --name local-mongo --uri mongodb://localhost:27017/analytics --username mongouser

# MongoDB Atlas profile (SRV URI)
support profile add --type mongodb --name atlas-mongo --uri "mongodb+srv://cluster.example.net/production?retryWrites=true" --username atlasuser
```
Supported `--type` options: `postgresql`, `mysql`, `mariadb`, `sqlite`, `sqlserver`, `mongodb`.
Optional flags depending on database type:
- `--host`: Database host (default: `localhost`).
- `--port`: Database port (default: database standard port, e.g. 5432 for PostgreSQL, 3306 for MySQL/MariaDB, 1433 for SQL Server).
- `--ssl-mode`: SSL mode:
  - PostgreSQL: `disable`, `allow`, `prefer`, `require`, `verify-ca`, `verify-full`.
  - MySQL: `disabled`, `preferred`, `required`, `verify_ca`, `verify_identity`.
  - MariaDB: `disabled`, `prefer`, `require`, `verify-ca`, `verify-full`.
- `--encrypt` / `--no-encrypt`: Whether to require encryption for SQL Server (default: true).
- `--trust-server-certificate` / `--no-trust-server-certificate`: Whether to trust server certificate without validation for SQL Server (default: false).
- `--database-path`: File path for SQLite.
- `--uri`: MongoDB deployment connection string (standard `mongodb://` or DNS SRV `mongodb+srv://`). Must not include embedded credentials or secret-bearing query options (such as `tlsCertificateKeyFilePassword` or `authMechanismProperties`).
- Target Database vs. Authentication Database (`authSource`):
  - Target database (`database`): Selected via URI path (e.g. `/analytics`) or `--database` (defaults to `admin`).
  - Authentication database (`authSource`): Set explicitly via URI option (e.g. `?authSource=admin`). When omitted, defaults to the path database (or `admin` if URI has no path). Target database does not silently overwrite explicit `authSource`.

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

# Test Microsoft SQL Server profile
support profile test local-mssql

# Test MongoDB profile
support profile test local-mongo
```

- **Credential Handling**: Automatically uses a stored password if present for client-server databases (PostgreSQL, MySQL, MariaDB, SQL Server). If no saved password exists and the command is run interactively, securely prompts for a one-time password (hidden input, press Enter for passwordless attempt). If run non-interactively without a saved password, fails cleanly with instructions. Embedded databases (SQLite) bypass password resolution and prompts entirely.
- **Connection Lifecycle & Safety**:
  - Enforces finite timeouts (10 seconds) and executes `SELECT 1`.
  - For PostgreSQL: validates the exact SSL mode and reports server version via Psycopg 3.
  - For MySQL & MariaDB: validates TLS semantics (`disabled`, `preferred`/`prefer`, `required`/`require`, `verify_ca`/`verify-ca`, `verify_identity`/`verify-full`), uses system trust roots, disables `local_infile`, avoids loading client option files, and reports server version via PyMySQL.
  - For SQLite: validates the file signature (`SQLite format 3\0`), header completeness, and page size before opening; rejects zero-byte, truncated, or non-SQLite files; opens the main database in read-only mode (`mode=ro`); supports in-memory databases (`:memory:`); and never executes data-changing queries. (Note: WAL-mode databases may require or create internal `-wal` and `-shm` sidecars; `immutable=1` is intentionally avoided as it is unsafe for live concurrent databases).
  - For Microsoft SQL Server: maps parameters directly to keyword arguments (`Server="host,port"`, `Database`, `UID`, `PWD`, `Encrypt`, `TrustServerCertificate`), explicitly disables driver connection pooling (`mssql_python.pooling(enabled=False)`), enforces 10-second login timeout and 10-second statement timeout, and uses autocommit for read-only connectivity verification.
  - For MongoDB: enforces 10-second timeouts (`serverSelectionTimeoutMS`, `connectTimeoutMS`, `socketTimeoutMS`, `timeoutMS`) via PyMongo and executes `ping` on the administrative database (`client.admin.command("ping")`).
    - **Guarantees**: Verifies server selection, cluster connectivity, and authentication credentials when configured and required. The configured target database is recorded and displayed as profile metadata.
    - **Non-guarantees**: Target database existence, collection existence, and read/write permissions are NOT verified by this connection test. No collection enumeration, CRUD operations, or permission probes are executed. The `buildInfo` command is omitted, and server version remains unset in connection test results.
  - Connections are deterministically closed immediately in all cases.
- **Native Platform Prerequisites (SQL Server via `mssql-python`)**:
  - **Officially Supported Operating Systems**:
    - **Windows**: Windows 11 (x64, ARM64); Windows Server 2019, 2022, 2025 (x64).
    - **macOS**: macOS 15 and later (Apple Silicon ARM64, Intel x86_64).
    - **Linux**: Ubuntu (22.04, 24.04, 26.04; x64, ARM64), Debian (11, 12, 13; x64, ARM64), RHEL (8, 9; x64, ARM64), Alpine Linux (x64, ARM64).
  - **Available Package Wheels**:
    - Wheels are published on PyPI for `win_amd64`, `win_arm64`, `macosx_15_0_universal2`, `manylinux_2_28`/`manylinux_2_34` (x86_64, aarch64), and `musllinux_1_2`.
  - **Additional Runtime Library Requirements**:
    - **Windows**: Zero external dependencies needed (`pip install mssql-python` installs the complete DDBC runtime).
    - **macOS**: Requires system OpenSSL (`brew install openssl`).
    - **Linux**: Requires OpenSSL and system libraries for dynamic linking and Kerberos:
      - Ubuntu/Debian: `sudo apt-get install libltdl7 libkrb5-3 libgssapi-krb5-2`
      - RHEL/CentOS/Fedora/Rocky: `sudo dnf install libtool-ltdl krb5-libs`
      - Alpine: `apk add libltdl krb5-libs libstdc++`
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

## Installation

### Core Installation
Support can be installed with minimal core dependencies:
```bash
pip install support-db
```
The core installation provides full profile management (create, list, show, rename, remove), OS-native secure credential storage, and local SQLite connection testing using Python's standard library. No external database drivers are required.

### Database Driver Extras
To test connections against external client-server databases, install the corresponding optional extras:

```bash
# PostgreSQL (via Psycopg 3 binary)
pip install "support-db[postgres]"

# MySQL & MariaDB (via pure-Python PyMySQL with RSA auth)
pip install "support-db[mysql]"

# Microsoft SQL Server (via official mssql-python)
pip install "support-db[sqlserver]"

# MongoDB (via official pymongo)
pip install "support-db[mongodb]"

# All database drivers combined
pip install "support-db[all]"
```

> **Note on SQLite**:
> SQLite connectivity is powered entirely by Python's built-in `sqlite3` standard library module. It requires no additional driver package or extra to be installed.

> **Important Disclaimer on Database Servers**:
> Installing an optional database driver extra (or `support-db[all]`) installs only the client driver libraries required to communicate with that database. **It does not install, configure, or run the database server itself.** You must have network access and appropriate credentials to a running database server instance (e.g., hosted locally, in Docker containers, or on remote servers).

### Missing Driver Errors & Remediation
Support uses lazy loading for all external database connectors. If you attempt to test a connection for a database type whose optional driver extra has not been installed, Support will fail cleanly with an actionable instruction pointing to the required extra:
```
PostgreSQL driver 'psycopg' is not installed. Install it using: pip install "support-db[postgres]"
```
If the driver package is installed but fails to load due to missing operating system libraries (such as missing C runtime or Kerberos libraries), Support reports the underlying import error as a connection error without claiming the package is uninstalled.

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
