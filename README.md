# Support — Multi-Database Natural Language to SQL
 
**Support** is a CLI-first AI database workspace built with Python, Typer, Rich, and Ollama/cloud LLM providers.
It provides normalized schema discovery and inspection across PostgreSQL, MySQL, SQLite, Microsoft SQL Server, and MongoDB, secure OS-level credential management, an LLM gateway, and an interactive terminal workspace.


---

## Architecture Overview

```
src/support/
├── core/                  # Configuration, exceptions, logging, and dialect definitions
│   ├── config.py          # AppConfig and environment loader
│   ├── dialect.py         # Dialect enum (PostgreSQL, MySQL, SQLite, MSSQL, Unknown)
│   ├── exceptions.py      # Domain exceptions hierarchy
│   └── logging.py         # Rich-formatted structured logger
├── db/                    # Connection layer, profile persistence, and credential security
│   ├── models.py          # ConnectionProfile data model (non-secret metadata)
│   ├── keyring_store.py   # OS Keyring credential storage with env var fallbacks
│   ├── manager.py         # ProfileManager: saves, loads, lists, and deletes profiles
│   ├── connection.py      # DatabaseConnectionFactory: builds SQLAlchemy engines safely
│   ├── health.py          # Unified health checks, status classification, concurrent probing
│   ├── service.py         # DatabaseService: decoupled reusable database operations
│   └── url_parser.py      # Connection URL parser and profile name inference
├── schema/                # Normalized schema representation & source adapters
│   ├── models.py          # DatabaseSchema, TableSchema, ColumnSchema, ForeignKeySchema, UniqueConstraintSchema
│   ├── mongo_models.py    # MongoDatabaseSchema, MongoCollectionSchema, MongoFieldSchema, MongoIndexSchema
│   ├── base.py            # BaseSchemaSource, SchemaDiscoveryResult, DiscoveryWarning
│   ├── sql_parser.py      # SQLGlot-based DDL parser & heuristic dialect detector
│   ├── merger.py          # Deterministic multi-file / multi-source schema merger
│   └── sources/
│       ├── sql_file.py    # DDL file(s) discovery source
│       ├── sqlite_db.py   # Read-only SQLite database file inspection source
│       ├── live_db.py     # Live relational database schema inspection via SQLAlchemy Inspector
│       └── mongo_db.py    # Live MongoDB document schema inspection via bounded sampling
├── project/               # Project-level directory traversal & heuristics
│   ├── scanner.py         # ProjectScanner (filtering .git, .venv, node_modules, etc.)
│   └── detector.py        # Dialect heuristics & migration framework clues
├── llm/                   # Multi-Provider LLM Gateway & Credential Store
│   ├── models.py          # Data models (LLMProviderType, ProviderStatus, GenerationResponse)
│   ├── config.py          # LLMConfig & LLMConfigManager (~/.support/llm_config.json)
│   ├── credentials.py     # Secure OS Keyring credential manager (Windows/macOS/Linux)
│   ├── base.py            # BaseLLMProvider abstract interface
│   ├── service.py         # Reusable LLMService application gateway
│   └── providers/         # Provider adapters
│       ├── ollama.py      # Local Ollama adapter with dynamic model discovery
│       ├── grok.py        # xAI Grok REST API adapter
│       └── gemini.py      # Google Gemini REST API adapter
├── workspace/             # Interactive Terminal Workspace
│   ├── state.py           # WorkspaceSessionState (in-memory tracking)
│   ├── display.py         # Rich headers, status banners, and help rendering
│   ├── commands.py        # Slash command handlers (/help, /db, /model, /clear, /exit)
│   └── session.py         # Session orchestration and REPL loop
└── cli/                   # Typer & Rich command-line application
    ├── main.py            # CLI entrypoint (scan, inspect-sqlite, version, db, models, auth)
    ├── db_cmds.py         # Concise database CLI commands (add, list, test, inspect, remove)
    ├── models_cmds.py     # LLM Gateway CLI commands (models, current, use, test)
    ├── auth_cmds.py       # Secure authentication commands (login, status, test, logout)
    ├── formatters.py      # Professional, concise CLI output formatting and tables
    └── display.py         # Rich schema inspection renderers
```

---

## 1. Local Docker Development Environment

Support provides a complete `docker-compose.yml` for local development containing **PostgreSQL**, **MySQL**, and **Microsoft SQL Server**. SQLite remains file-based and runs locally without containers.

To avoid conflicts with existing database services on your machine, development containers bind to localhost (`127.0.0.1`) on non-standard ports by default.

### Default Port & Credential Matrix

| Service | Container Name | Localhost Port | Database | Username | Password (Dev Only) |
|---|---|---|---|---|---|
| **PostgreSQL** | `support_dev_postgres` | `127.0.0.1:5435` | `support_dev` | `support_dev` | `dev_pg_pass_2026` |
| **MySQL** | `support_dev_mysql` | `127.0.0.1:3308` | `support_dev` | `support_dev` | `dev_mysql_pass_2026` |
| **MS SQL Server** | `support_dev_mssql` | `127.0.0.1:1435` | `support_dev` | `sa` | `DevPass_2026!` |

### Managing Docker Services

```powershell
# Start all services in the background
docker compose up -d

# Start an individual database (e.g., PostgreSQL)
docker compose up -d postgres

# Check container health and status
docker compose ps

# View container logs
docker compose logs -f postgres

# Stop services without deleting database volumes
docker compose down

# Stop services and remove persistent volumes
docker compose down -v
```

> **Automated Sample Schema**: Each Docker container mounts automated schema seeds from `docker/init/<engine>/01_init.sql` on first startup, providing sample `organizations`, `users`, `orders`, and `order_items` tables with foreign-key relationships.

---

## 2. Saved Connection Profiles

Support allows you to save and manage connection profiles for multiple database instances (both local Docker instances and remote authorized database servers).

### Security Architecture

- **Non-secret metadata** (name, dialect, host, port, database, username, options) is stored in JSON format in the user configuration directory (`~/.support/profiles.json` or `$SUPPORT_CONFIG_DIR`).
- **Passwords are never stored in plaintext files, source code, or logs**. Passwords are encrypted and persisted into the operating system's native credential store:
  - **Windows**: Windows Credential Manager (`WinVaultKeyring`)
  - **macOS**: Keychain
  - **Linux**: SecretService / KWallet
- **Environment Variable Fallback**: If an OS keyring is unavailable, passwords can be passed securely using environment variables (`SUPPORT_DB_PASSWORD_<PROFILE_NAME>`).
- **Redacted Connection Strings**: Connection URLs printed in logs and CLI outputs always mask credentials (`***`).

---

## 3. CLI Command Reference

### Connection Profile Commands (`support db`)

#### 1. Add a Profile (Interactive or Flag-based)

```powershell
# Interactive mode (prompts for dialect, host, database, user, and hidden password):
support db add

# 1. Add via Connection URL (Recommended)
support db add --url "postgresql://postgres:postgres@localhost:5433/athena" --name athena

# Omit --name to automatically infer the profile name from the database name:
support db add --url "postgresql://support_dev:dev_pg_pass_2026@127.0.0.1:5435/support_dev"

# SQLite URL:
support db add --url "sqlite:///examples/sample.db"

# 2. Add via Interactive Prompts (with hidden password input):
support db add

# 3. Add via Individual Flags:
support db add --name dev-mysql --dialect mysql --host 127.0.0.1 --port 3308 --database support_dev --username support_dev --password dev_mysql_pass_2026

# Skip live connectivity testing before saving:
support db add --url "postgresql://user:pass@remote:5432/db" --no-test
```

> **Security Note**: Credentials provided inside command-line arguments can be recorded in shell history. For sensitive environments, omit passwords to use hidden interactive prompts, or reference environment variables using `--password-env`.

#### 2. List Saved Profiles & Check Live Status

```powershell
# Probe all saved databases concurrently and display live connectivity status:
support db list

# Display profiles instantly without probing connections:
support db list --no-check

# Adjust per-database connection probe timeout:
support db list --timeout 2.0
```

`support db list` concurrently checks each database endpoint using a bounded thread pool and displays live statuses:
- **`ONLINE`**: Connection verified, accompanied by latency in milliseconds.
- **`OFFLINE`**: Host or port endpoint is unreachable.
- **`AUTH FAILED`**: Host was reached, but credentials were rejected.
- **`TIMEOUT`**: Connection exceeded probe timeout limit.
- **`ERROR`**: Database-specific driver or protocol failure.

#### 3. Test Connection

```powershell
support db test dev-pg
```

Tests database connectivity, displays network latency (ms), and prints the database engine version.

#### 4. Inspect Live Database Schema

```powershell
# Formatted Rich terminal view:
support db inspect dev-pg

# Export normalized schema as JSON:
support db inspect dev-pg --json > schema.json
```

Discovers all tables, columns, data types, nullability, primary keys, unique constraints, and foreign-key relationships directly from the live database.

#### 5. Remove a Profile

```powershell
# Prompts for confirmation:
support db remove dev-pg

# Non-interactive removal:
support db remove dev-pg --yes
```

Removes the profile configuration and deletes the credential from the OS keyring without altering the underlying database.

---

### Offline Discovery Commands

```powershell
# Scan project directory for raw SQL DDL migration files:
support scan path/to/project

# Scan project with explicit dialect:
support scan path/to/project --dialect postgres

# Inspect a SQLite database file directly in read-only mode:
support inspect-sqlite path/to/app.sqlite

# Check version:
support --version
```

---

## 4. Multi-Provider LLM Gateway

Support provides a modular, provider-agnostic LLM Gateway designed for natural language generation. The gateway decouples the application from provider-specific SDKs, returning normalized responses with content, token counts, and latency metrics.

### Supported Providers

| Provider | Type | Default Model | Authentication / Config | Discovery |
|---|---|---|---|---|
| **Ollama** *(Default)* | Local | `qwen2.5-coder:7b` | `OLLAMA_BASE_URL` (default: `http://localhost:11434`) | Dynamic `/api/tags` |
| **Grok** | Cloud API | `grok-2-latest` | `support auth login grok` (fallback: `XAI_API_KEY`) | Dynamic `/models` + fallback |
| **Gemini** | Cloud API | `gemini-2.0-flash` | `support auth login gemini` (fallback: `GEMINI_API_KEY`) | Dynamic `/models` + fallback |

### Secure Credential Management (`support auth`)

Support provides built-in, secure API key management powered by Python's `keyring` library and operating system credential stores:
- **Windows**: Windows Credential Manager (`WinVaultKeyring`)
- **macOS**: Apple Keychain
- **Linux**: Secret Service / KWallet

API keys are **never** persisted in configuration files, plaintext files, or printed to terminal output.

```powershell
# Authenticate a provider (prompts for API key with hidden input and validates before saving):
support auth login gemini
support auth login grok

# Check configured providers and storage backend:
support auth status

# Test stored credentials and provider API connectivity without changing configuration:
support auth test gemini
support auth test grok

# Remove credentials from the OS keyring:
support auth logout gemini
```

### LLM CLI Commands (`support models`)

```powershell
# List available and configured models across all providers:
support models

# Show currently active provider, model, and generation parameters:
support models current

# Switch default provider and model:
support models use ollama --model qwen2.5-coder:7b
support models use grok --model grok-2-latest
support models use gemini --model gemini-2.0-flash

# Test current default provider and model with a quick ping:
support models test

# Test a specific provider/model without changing default configuration:
support models test ollama --model qwen2.5:0.5b
support models test grok
support models test gemini
```

### Configuration & Security
- **Config file**: Persistent non-secret settings (default provider, default model, base URLs, temperature, timeout) are saved to `~/.support/llm_config.json` (or `$SUPPORT_CONFIG_DIR`).
- **Secret handling**: API keys are securely stored in the OS credential store with environment variables (`XAI_API_KEY`, `GEMINI_API_KEY`) supported as optional fallbacks for CI/CD and headless environments.
- **Fail-safe defaults**: Ollama is the primary default provider. If Ollama is unavailable, clear guidance is shown rather than silently falling back to a paid cloud provider.

---

## 5. Interactive Terminal Workspace

Running `support` without arguments launches a fast, persistent interactive terminal workspace designed for AI database exploration. Startup is near-instantaneous (< 5ms) because configuration and profile metadata are loaded locally without synchronous network blocking.

```powershell
# Launch the interactive workspace
support
```

### Workspace Features:
- **Fast Startup**: Instantly renders workspace header without blocking on remote database pings or LLM connectivity.
- **Branding & Status Banner**: Displays active database connection status, active LLM provider/model, and guidance.
- **Session State**: Explicit in-memory tracking of active database profile, LLM provider/model, conversation history, and current task.
- **Guided Database Onboarding**: Configure databases directly from inside the workspace via `/db` (URL or step-by-step with masked password input).
- **Session-Scoped Customization**: Changes made inside the workspace remain session-scoped and do not unintentionally overwrite persistent defaults.
- **Graceful Termination**: Supports `Ctrl+C` (`KeyboardInterrupt`) and `EOF` (`Ctrl+D`/`Ctrl+Z`) for clean exits without unhandled tracebacks.

### Workspace Slash Commands:

| Command | Action |
|---|---|
| `/help` | Explains available workspace commands and usage guidance. |
| `/db` | Lists saved database profiles with health status, lets you switch or add a profile via guided setup. |
| `/model` | Displays available providers and models, letting you switch the active LLM for this session. |
| `/clear` | Clears the visible terminal screen and redisplays the workspace header without losing session state. |
| `/exit` or `/quit` | Exits the interactive workspace cleanly. |

---

## 6. Running Tests

Run the complete test suite using pytest in the virtual environment:

```powershell
.\.venv\Scripts\pytest -v
```

All unit and integration tests cover:
- DDL parsing and dialect heuristics
- Project scanning and directory pruning
- SQLite file read-only metadata inspection and nonexistent file safety
- Connection profiles lifecycle and password security
- Database connection health checking, driver verification, and status classification
- Reusable DatabaseService operations
- Live database schema inspection across PostgreSQL, MySQL, SQLite, and MS SQL Server
- Live MongoDB document inspection via bounded document sampling
- Composite primary keys, composite unique constraints, and foreign key cascades
- Multi-provider LLM Gateway (Ollama, Grok, Gemini adapters)
- LLM response normalization, timeout handling, and token usage tracking
- Secure OS Keyring credential storage, validation before saving, and env var fallbacks
- Secret redaction and credential logout
- Interactive terminal workspace session loop, header display, slash commands (`/help`, `/db`, `/model`, `/clear`, `/exit`), input handling, and clean interrupt exits
- Typer CLI command suite (`scan`, `inspect-sqlite`, `db *`, `models *`, `auth *`)

---

## Supported Engines & Drivers

| Engine | Driver Package | Connection Scheme | Status |
|---|---|---|---|
| **PostgreSQL** | `psycopg` (v3) | `postgresql+psycopg://` | Verified & Live Tested |
| **MySQL** | `pymysql` | `mysql+pymysql://` | Verified & Live Tested |
| **SQLite** | `sqlite3` (built-in) | `sqlite:///` | Verified & Live Tested |
| **MS SQL Server** | `pymssql` | `mssql+pymssql://` | Verified & Live Tested |
| **MongoDB** | `pymongo` | `mongodb://`, `mongodb+srv://` | Verified & Tested |

---

## Current Scope & Limitations

- **Read-Only Live Inspection**: Live inspection only queries database metadata, schemas, and bounded document samples; it does not modify schemas or execute arbitrary user queries.
- **Interactive Workspace Foundation**: The interactive REPL loop, state management, slash commands, guided database onboarding, and status displays are complete; natural-language query generation and validation are scheduled for an upcoming release.
- **Frontend / Web Interface**: The application currently operates as an interactive terminal workspace, standalone CLI, and reusable Python library.

