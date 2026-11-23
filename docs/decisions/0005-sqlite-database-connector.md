# 5. Direct SQLite Connection Testing via Standard Library sqlite3

## Context

Support requires verification of SQLite database connectivity (`support profile test <NAME>`) alongside PostgreSQL.
Unlike client-server database engines (e.g., PostgreSQL, MySQL), SQLite is an embedded file-based (or in-memory) database.

Testing SQLite connections introduces specific operational considerations:
1. **Zero Side Effects on Missing Databases**: Opening a non-existent path in standard `sqlite3` without read-only flags creates an empty database file. Connection testing must never create missing database files.
2. **Main Database Read-Only Mode**: Connection testing opens the main database file in read-only mode (`mode=ro`). Support does not execute data-changing queries or modify application data.
3. **WAL Sidecar Operation**: For databases operating in Write-Ahead Logging (WAL) mode, SQLite WAL operation may require or create internal `-wal` and `-shm` (shared-memory index) sidecars depending on the database state and filesystem permissions.
4. **Rejection of `immutable=1` Workaround**: Setting `immutable=1` is not used as a universal workaround. The SQLite specification notes that `immutable=1` assumes the database resides on frozen, read-only media without concurrent writers; applying it to live databases risks reading stale or corrupted data if other processes write to the database.
5. **Pre-flight File Validation**: `PRAGMA schema_version` and `SELECT 1` in SQLite can evaluate on zero-byte files without raising errors. A robust pre-flight check must validate the SQLite file signature (`SQLite format 3\0`), header completeness, and encoded page size.
6. **Integrity Scope**: Validating a recognized SQLite header confirms structural header validity, but does not guarantee every database page is healthy. Connection testing avoids full database integrity scans (`PRAGMA integrity_check`) to maintain fast, non-intrusive operations.
7. **No Credential Requirements**: SQLite does not natively use usernames or passwords; credential prompts or lookups are completely bypassed.

## Decision

We implement `SQLiteConnector` in `support.infrastructure.sqlite_connector` using Python's standard-library `sqlite3`.

### 1. Pre-Flight File & Header Validation
Before opening a file-backed database:
- Path checks: Confirms the path exists (`DatabaseNotFoundError` if missing) and is not a directory (`GeneralConnectionError`).
- Permission checks: Confirms the file has read permissions (`GeneralConnectionError` if unreadable). Local filesystem permission failures map to general connection/file-access errors, not host reachability errors.
- Size and header completeness:
  - Rejects zero-byte files (`file_size == 0`).
  - Rejects files smaller than the 100-byte SQLite header (`file_size < 100`).
- Signature check:
  - Validates the 16-byte magic header: `b"SQLite format 3\x00"`.
- Page size validation:
  - Validates the 2-byte big-endian integer at offset 16.
  - Page size must be a power of two between 512 and 32768, or 65536.
  - Rejects files smaller than their declared page size (`file_size < page_size`).
- Note: A recognized SQLite header does not guarantee every database page is healthy. Connection testing does not perform a complete integrity scan.

### 2. Read-Only File Opening via URI
- For file-backed databases, `SQLiteConnector` resolves the path and opens the database using SQLite URI parameters:
  `f"{resolved_path.as_uri()}?mode=ro"` with `uri=True`.
- This ensures:
  - The main database file is opened in read-only mode.
  - Support executes strictly non-mutating validation queries (`PRAGMA schema_version;`, `SELECT 1`).
  - Missing database files are never created.
  - Note: WAL-mode databases may still access or create internal `-wal` and `-shm` sidecars as part of standard SQLite concurrency control. `immutable=1` is intentionally avoided to preserve correctness under concurrent access.

### 3. In-Memory Database Support
- The special path `:memory:` is recognized and supported.
- In-memory databases are opened directly via `sqlite3.connect(":memory:", timeout=10.0)`.

### 4. Deterministic Connection Lifecycle
- Connections are wrapped in a strict `try...finally` block that guarantees calling `connection.close()` unconditionally, ensuring file descriptors and locks are immediately released.

### 5. Categorized Error Translation
Native `sqlite3` exceptions are translated into Support's domain/application errors:
- Missing file: `DatabaseNotFoundError` ("SQLite database file not found: ...")
- Path is directory: `GeneralConnectionError` ("Provided path is a directory, not an SQLite database file: ...")
- Permission denied / unreadable: `GeneralConnectionError` ("Access denied or unreadable SQLite database file: ...")
- Invalid/corrupted database file (`sqlite3.DatabaseError`): `GeneralConnectionError` ("Invalid or malformed SQLite database: ...")
- Lock/busy timeout: `ConnectionTimeoutError` ("SQLite database is locked or busy: ...")
- Other operational failures: `GeneralConnectionError`

### 6. Connector Dispatch Integration
- `ConnectorDispatcher` routes `SQLiteConnection` directly to `SQLiteConnector`.
- `ConnectionTestWorkflow` and Typer CLI `support profile test` bypass credential retrieval and prompts when `profile.connection.database_type == DatabaseType.SQLITE`.

## Consequences

- Lightweight, fast, and completely dependency-free SQLite connection testing.
- Accurate rejection of zero-byte, truncated, and non-SQLite files prior to opening.
- Clear distinction between network host reachability (`HostUnreachableError` for PostgreSQL) and local filesystem access failures (`GeneralConnectionError` for SQLite).
- Realistic, accurate documentation of SQLite read-only and WAL operation without unsafe `immutable=1` flags.
