# Support — Multi-Database Natural Language to SQL & Terminal Workspace

**Support** is a CLI-first AI database workspace built with Python, Typer, Rich, SQLGlot, and Ollama/cloud LLM providers.
It provides normalized schema discovery across PostgreSQL, MySQL, SQLite, Microsoft SQL Server, and MongoDB, conversational natural-language SQL generation and mutations, AST-level safety enforcement, automatic execution error repair, adaptive result rendering, secure OS-level credential management, and an interactive terminal workspace.

---

## Key Features

- **Conversational Database Operations**: Generate, validate, and execute read-only queries (`SELECT`) and mutations/DDL (`INSERT`, `UPDATE`, `DELETE`, `CREATE TABLE`, `ALTER TABLE`, `CREATE INDEX`) directly from natural language.
- **AST Safety & Schema Verification**: SQL statements are parsed and verified using SQLGlot against the live database schema, preventing destructive injection, invalid syntax, and unconstrained queries.
- **Automatic Execution Error Repair**: If a query fails execution on the database (e.g. dialect syntax error, column mismatch), Support automatically re-prompts the LLM with the error message and schema context to repair the statement and prompt for execution approval.
- **Adaptive Result Layouts**: Query results automatically adapt to terminal width, displaying responsive Rich tables for compact results and detailed record cards for wide tables.
- **Sensitive Data Redaction**: Credential and secret fields (`password`, `token`, `secret`, `api_key`) are masked across all table views and record renders.
- **Multi-Database Support**: First-class discovery and execution for PostgreSQL (`psycopg` v3), MySQL (`pymysql`), SQLite (`sqlite3`), Microsoft SQL Server (`pymssql`), and MongoDB (`pymongo`).
- **Interactive Terminal Workspace**: Launch `support` without arguments for an instant, persistent REPL with live status banners, schema inspection (`/schema`), Mermaid ER diagrams (`/diagram`), query history (`/history`), diagnostics (`/details`), and profile switching (`/db`).
- **Persistent Profiles & Defaults**: Saved database profiles persist in configuration; selecting an active profile in the workspace retains it as default across sessions.
- **Secure Keyring Storage**: Passwords and LLM API keys are encrypted in OS native credential stores (Windows Credential Manager, Apple Keychain, Linux Secret Service).
- **Multi-Provider LLM Gateway**: Local Ollama (default), Google Gemini, and xAI Grok with live model discovery, zero-latency caching, and connectivity testing.

---

## Architecture Overview

```
src/support/
├── core/                  # Configuration, formatting, exceptions, logging, and dialect definitions
│   ├── config.py          # AppConfig and environment loader
│   ├── dialect.py         # Dialect enum (PostgreSQL, MySQL, SQLite, MSSQL, MongoDB)
│   ├── exceptions.py      # Domain exceptions hierarchy
│   ├── formatting.py      # Duration, timestamp, and size formatters
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
│   ├── diagram.py         # Mermaid entity-relationship (ER) diagram generator
│   ├── sql_parser.py      # SQLGlot-based DDL parser & heuristic dialect detector
│   ├── merger.py          # Deterministic multi-file / multi-source schema merger
│   └── sources/
│       ├── sql_file.py    # DDL file(s) discovery source
│       ├── sqlite_db.py   # Read-only SQLite database file inspection source
│       ├── live_db.py     # Live relational database schema inspection via SQLAlchemy Inspector
│       └── mongo_db.py    # Live MongoDB document schema inspection via bounded sampling
├── query/                 # Query generation, AST validation, execution & repair
│   ├── models.py          # QueryDraft and QueryResult data models
│   ├── generator.py       # QueryGenerator: LLM prompt construction, draft generation & recovery
│   ├── validator.py       # SQLGlot AST validation, injection safety & schema verification
│   ├── executor.py        # ReadOnlyQueryExecutor: driver-level guards, limits, timeouts
│   ├── privacy.py         # Sensitive column detection, wildcard projection expansion & redaction
│   ├── repair.py          # Schema-grounded spelling correction, prompt repair & error classification
│   ├── inspection.py      # Single- and multi-table natural-language schema inspection
│   ├── schema_context.py  # Live schema inspection and prompt context serialization
│   └── display.py         # Rich query draft, execution preview, and adaptive result tables
├── mutation/              # Structured DDL and mutation execution
│   ├── models.py          # MutationPlan, MutationType, and MutationResult
│   ├── planner.py         # Safe mutation planning and prerequisite resolution
│   ├── executor.py        # Transactional mutation executor
│   ├── workflow.py        # Interactive confirmation and transaction lifecycle
│   └── sample_data.py     # Grounded sample record generation for testing
├── history/               # Persistent query history and replay
│   ├── models.py          # HistoryEntry data model
│   ├── store.py           # SQLite-based persistent query history store
│   └── commands.py        # /history slash command handlers (list, use, clear)
├── tools/                 # Tool selection and dispatch
│   ├── registry.py        # Tool registry and metadata
│   ├── selector.py        # Natural-language tool selector
│   └── executor.py        # Multi-tool execution dispatch
├── llm/                   # Multi-Provider LLM Gateway & Credential Store
│   ├── models.py          # Data models (LLMProviderType, ProviderStatus, GenerationResponse)
│   ├── config.py          # LLMConfig & LLMConfigManager (~/.support/llm_config.json)
│   ├── credentials.py     # Secure OS Keyring credential manager
│   ├── base.py            # BaseLLMProvider abstract interface
│   ├── service.py         # Reusable LLMService application gateway
│   └── providers/         # Provider adapters (Ollama, Grok, Gemini)
├── workspace/             # Interactive Terminal Workspace
│   ├── state.py           # WorkspaceSessionState (in-memory tracking & defaults persistence)
│   ├── session.py         # Session orchestration, REPL loop, and query dispatch
│   ├── commands.py        # Slash command routing and dispatcher
│   ├── general_commands.py# /help, /clear, /status, /schema handlers
│   ├── db_commands.py     # /db selection, profile switching, and onboarding
│   ├── model_commands.py  # /model selection and configuration
│   ├── execution_commands.py # Conversational execution, /run, and post-execution recovery
│   ├── display.py         # Rich headers, status banners, and help tables
│   └── checker.py         # Background concurrent health and connectivity checker
└── cli/                   # Typer & Rich command-line application
    ├── main.py            # CLI entrypoint (scan, inspect-sqlite, version, db, models, auth)
    ├── db_cmds.py         # Database CLI commands (add, list, test, inspect, remove)
    ├── models_cmds.py     # LLM Gateway CLI commands (models, current, use, test)
    ├── auth_cmds.py       # Authentication commands (login, status, test, logout)
    └── formatters.py      # Rich terminal formatting and tables
```

---

## 1. Local Docker Development Environment

Support provides a `docker-compose.yml` for local development containing **PostgreSQL**, **MySQL**, and **Microsoft SQL Server**. SQLite runs locally without containers.

Development containers bind to localhost (`127.0.0.1`) on non-standard ports to avoid conflicts:

| Service | Container Name | Localhost Port | Database | Username | Password (Dev Only) |
|---|---|---|---|---|---|
| **PostgreSQL** | `support_dev_postgres` | `127.0.0.1:5435` | `support_dev` | `support_dev` | `dev_pg_pass_2026` |
| **MySQL** | `support_dev_mysql` | `127.0.0.1:3308` | `support_dev` | `support_dev` | `dev_mysql_pass_2026` |
| **MS SQL Server** | `support_dev_mssql` | `127.0.0.1:1435` | `support_dev` | `sa` | `DevPass_2026!` |

```powershell
# Start all services in the background
docker compose up -d

# Start an individual database (e.g., PostgreSQL)
docker compose up -d postgres

# Check container health and status
docker compose ps

# Stop services without deleting database volumes
docker compose down

# Stop services and remove persistent volumes
docker compose down -v
```

> **Automated Sample Schema**: Each Docker container mounts schema seeds from `docker/init/<engine>/01_init.sql` on first startup, creating sample `organizations`, `users`, `orders`, and `order_items` tables with foreign-key relationships.

---

## 2. Interactive Terminal Workspace

Launching `support` without arguments opens the interactive terminal workspace:

```powershell
support
```

```text
╭──────────────────────────────────────────────────────────────────────────────╮
│  Support — Multi-Database AI Workspace                                v0.1.0 │
╰──────────────────────────────────────────────────────────────────────────────╯
Database: dev-pg (postgres) · ONLINE (4.2ms)
Model:    ollama / qwen2.5-coder:7b · ONLINE

Type /help for commands, /exit to quit.
Example queries:
  "Show the top 5 customers by total order amount"
  "List all orders placed in the last 30 days"
  "create a table called students with id, name, and email"
```

### Conversational Workflow & Execution Safeguards

1. **Natural Language Input**: Type questions or commands directly at the prompt.
2. **Draft Preview**: Displays formatted SQL with syntax highlighting, target dialect, schema alignment status, and explanation.
3. **Execution Approval (`[y/N]`)**: Queries require explicit confirmation before execution:
   - **Read-only queries**: Executed with enforced row limits (max 100) and timeouts (10s).
   - **Mutations & DDL**: Executed within an isolated transaction; automatically refreshes the schema cache upon commit.
4. **Adaptive Table & Record Rendering**: Compact result sets render as clean Rich tables. Wide result sets render as individual record cards. Sensitive fields (`password_hash`, tokens) are automatically redacted.
5. **Automatic Error Recovery**: If an execution failure occurs (such as a database syntax error or unknown column), Support automatically sends the error to the LLM for repair, displays the proposed correction, and prompts for approval to execute the fix.

### Workspace Slash Commands

| Command | Description |
|---|---|
| `/status [refresh]` | Display or re-verify active database and LLM model connectivity. |
| `/schema [refresh]` | View discovered tables, column counts, data types, and foreign keys in a structured table. |
| `/diagram [tables]` | Generate a Mermaid entity-relationship (ER) diagram for the active database. |
| `/details` | Inspect AST validation status, dialect details, and schema coverage for the active draft. |
| `/history [use N\|clear]` | Search past queries, view execution durations and row counts, or restore a prior draft. |
| `/run [table\|vertical]` | Re-execute or re-render the latest query result in tabular or vertical record layout. |
| `/db [name]` | Switch the active database profile (persisting as default across restarts) or add a new profile. |
| `/model` | Inspect and switch the active LLM provider and model for the session. |
| `/clear` | Clear the terminal screen while preserving session state. |
| `/exit`, `/quit` | Exit the interactive workspace cleanly. |

---

## 3. Database Connection Profiles (`support db`)

### Add a Profile

```powershell
# 1. Add via Connection URL (Recommended):
support db add --url "postgresql://support_dev:dev_pg_pass_2026@127.0.0.1:5435/support_dev" --name dev-pg

# SQLite URL:
support db add --url "sqlite:///examples/sample.db" --name local-sqlite

# 2. Add via Guided Interactive Prompts (with masked password input):
support db add

# 3. Add via Individual Flags:
support db add --name dev-mysql --dialect mysql --host 127.0.0.1 --port 3308 --database support_dev --username support_dev --password dev_mysql_pass_2026
```

### List Profiles & Check Health

```powershell
# Probe all saved profiles concurrently with live status and latency:
support db list

# Display profiles without network probing:
support db list --no-check
```

### Test & Inspect

```powershell
# Test connectivity and retrieve engine version:
support db test dev-pg

# Inspect live schema in terminal:
support db inspect dev-pg

# Export normalized schema as JSON:
support db inspect dev-pg --json > schema.json

# Remove a saved profile:
support db remove dev-pg
```

---

## 4. Multi-Provider LLM Gateway

Support includes a provider-agnostic LLM Gateway decoupling the application from specific SDKs.

### Supported Providers

| Provider | Type | Default Model | Configuration / Auth | Discovery |
|---|---|---|---|---|
| **Ollama** *(Default)* | Local | `qwen2.5-coder:7b` | `OLLAMA_BASE_URL` (default: `http://localhost:11434`) | Dynamic `/api/tags` |
| **Grok** | Cloud API | `grok-2-latest` | `support auth login grok` (fallback: `XAI_API_KEY`) | Dynamic `/models` |
| **Gemini** | Cloud API | `gemini-2.0-flash` | `support auth login gemini` (fallback: `GEMINI_API_KEY`) | Dynamic `/models` |

### Credential Management (`support auth`)

API keys are stored encrypted in the operating system's native credential store:

```powershell
# Authenticate a provider (prompts for API key with hidden input and validates before saving):
support auth login gemini
support auth login grok

# Check configured providers and storage backend:
support auth status

# Test stored credentials without changing configuration:
support auth test gemini

# Remove credentials from the OS keyring:
support auth logout gemini
```

### Model Management (`support models`)

```powershell
# List available models across all providers:
support models

# Show currently active provider, model, and generation parameters:
support models current

# Switch default provider and model:
support models use ollama --model qwen2.5-coder:7b
support models use grok --model grok-2-latest
support models use gemini --model gemini-2.0-flash

# Test current model with a ping:
support models test
```

---

## 5. Offline Discovery Commands

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

## 6. Supported Engines & Drivers

| Engine | Driver Package | Connection Scheme | Status |
|---|---|---|---|
| **PostgreSQL** | `psycopg` (v3) | `postgresql+psycopg://` | Verified & Live Tested |
| **MySQL** | `pymysql` | `mysql+pymysql://` | Verified & Live Tested |
| **SQLite** | `sqlite3` (built-in) | `sqlite:///` | Verified & Live Tested |
| **MS SQL Server** | `pymssql` | `mssql+pymssql://` | Verified & Live Tested |
| **MongoDB** | `pymongo` | `mongodb://`, `mongodb+srv://` | Verified & Tested |

---

## 7. Running Tests

Run the full test suite with pytest:

```powershell
$env:PYTHONPATH="src"
.\.venv\Scripts\pytest -v
```

The test suite covers:
- Natural-language query generation (SELECT, INSERT, UPDATE, DELETE, CREATE TABLE, ALTER TABLE, CREATE INDEX)
- SQLGlot AST validation, dialect syntax enforcement, and mutation guards
- Automatic post-execution LLM repair on database syntax/schema failures
- Sensitive data redaction, wildcard expansion guards, and privacy checks
- Interactive workspace REPL loop, prompt rendering, and slash commands (`/status`, `/schema`, `/diagram`, `/details`, `/history`, `/run`, `/db`, `/model`, `/clear`, `/exit`)
- DDL parsing, multi-source schema merging, and live schema inspection
- Persistent default database profile selection and OS Keyring credential storage
- Multi-provider LLM gateway (Ollama, Grok, Gemini) and response normalization
- Typer CLI command suite (`scan`, `inspect-sqlite`, `db *`, `models *`, `auth *`)
