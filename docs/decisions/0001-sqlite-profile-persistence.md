# 1. SQLite Profile Persistence

## Context

Support requires local persistence for database connection profiles (such as profile aliases, target database, username, host, port, and SSL negotiation mode). The storage mechanism needs to be lightweight, cross-platform, dependency-free, and capable of enforcing unique constraints and data integrity without running a background database service or introducing heavy ORM frameworks.

Support is currently under active pre-release development without public releases.

## Decision

We use Python's built-in `sqlite3` engine with the following characteristics:

1. **Storage Location**:
   - Default configuration path: `platformdirs.user_config_path("support", appauthor=False) / "profiles.sqlite3"`.
   - Environment override: `SUPPORT_HOME` environment variable overrides the root directory for `profiles.sqlite3`.
   - File permissions: On POSIX systems, permissions are restricted to user-only (`0o700` for the configuration directory, `0o600` for the database file). On Windows, standard user profile ACLs are relied upon.
   - Lazy creation: The database file and directory are created only on write operations; read operations on non-existent databases return empty results without creating empty files on disk.

2. **Canonical Schema**:
   - Exactly one canonical structure is defined for schema version 1:
     - `id TEXT PRIMARY KEY NOT NULL`
     - `name TEXT UNIQUE NOT NULL`
     - `database_type TEXT NOT NULL`
     - `connection_config TEXT NOT NULL`
   - SQLite `PRAGMA user_version` tracks the schema version (pre-release baseline is version 1).

3. **Pre-Release Migration Policy**:
   - Backward-compatibility migrations for unreleased development schemas are intentionally omitted prior to the first public release.
   - Obsolete development schemas (such as earlier PostgreSQL-only iterations or temporary development schemas) and incompatible databases are rejected with clear, actionable errors rather than silently repaired, altered, or claimed.
   - Developers working with incompatible local development databases must back up and recreate the database to adopt the current canonical schema.
   - Formal versioned migrations will be introduced when the application reaches public release and preserving end-user production data across upgrades becomes necessary.

4. **Security Boundary**:
   - SQLite stores profile connection metadata only.
   - Passwords, connection strings containing credentials, and secret tokens are strictly excluded from SQLite storage.

5. **Lifecycle & Concurrency**:
   - Short-lived connections opened on-demand per repository operation and closed immediately.
   - Busy timeout configured to 5000ms.
   - Atomic transactions (`with conn:`) for mutations.
   - Uniqueness conflicts on profile names handled via SQLite `UNIQUE` constraints and mapped to application errors.

## Consequences

- No external database daemons or ORM dependencies required.
- Fast, deterministic startup and test isolation using temporary directories.
- Clean pre-release codebase without speculative or transitional migration code.
- Clear separation between non-sensitive connection metadata (SQLite) and future credential storage mechanisms.
