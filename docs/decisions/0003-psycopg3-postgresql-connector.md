# 3. Direct PostgreSQL Connection Testing via Psycopg 3

## Context

Support requires the capability to verify database connectivity for saved database profiles (`support profile test <NAME>`).
To perform connection testing, Support must establish a live network connection to a PostgreSQL instance, verify access by executing a harmless validation query (`SELECT 1`), report the server status, and release all network and system resources cleanly.

We must choose an appropriate database client driver while ensuring that:
1. No passwords or secret credentials are exposed in command-line arguments, logs, error traces, or formatted connection URIs/DSNs.
2. The architectural design avoids premature ORM or abstraction overhead (such as SQLAlchemy or multi-engine abstraction layers).
3. The driver supports modern Python typing, active maintenance, binary wheels across platforms (Windows, macOS, Linux), and granular control over connection timeouts and SSL modes.

## Decision

We adopt **Psycopg 3** (`psycopg[binary]>=3.2,<4`) using its synchronous API as Support's direct PostgreSQL connector.

### 1. Rejection of SQLAlchemy and Heavy Frameworks
- Support currently requires focused, lightweight connectivity testing and subsequent targeted metadata queries.
- SQLAlchemy introduces significant complexity: engine management, connection pooling, dialect mappings, reflection caches, and ORM infrastructure. At this stage of development, a connection pool or ORM is unnecessary and hinders explicit, deterministic connection lifecycle control.
- Psycopg 3 provides a direct, standard DB-API implementation with modern Python features (e.g. native type annotations, clean context managers, binary package distribution).

### 2. Synchronous Connection Lifecycle
- CLI command executions are synchronous, bounded single-shot operations. The synchronous `psycopg.connect()` API is used.
- All connections are opened with an explicit `connect_timeout` of 10 seconds.
- Connections are managed within a `try...finally` lifecycle that guarantees immediate release (`conn.close()`) regardless of whether the test query succeeds or fails.

### 3. Connection Parameters and Credential Isolation
- Connections are established using explicit keyword arguments (`host`, `port`, `dbname`, `user`, `password`, `sslmode`, `connect_timeout`) passed directly to `psycopg.connect()`.
- Support strictly prohibits constructing connection URIs or DSN strings containing passwords, preventing accidental credential leakage in exception messages, process tables, or logs.
- The SSL mode configured on the profile (`disable`, `allow`, `prefer`, `require`, `verify-ca`, `verify-full`) is passed verbatim to the driver.

### 4. Credential Resolution Flow
When testing a profile connection:
1. Support first queries the OS credential store using the profile's immutable UUID.
2. If a saved password exists, Support uses it directly without user interaction.
3. If no saved password exists:
   - In interactive mode (terminal attached), Support securely prompts the user for a one-time password using hidden input. An empty entry denotes an attempt at a passwordless connection.
   - In non-interactive mode (pipes, scripts, automated CI), Support immediately fails with actionable instructions on how to persist credentials using `support profile password set <NAME>`.
4. One-time passwords are never cached, persisted to disk, or retained beyond the test execution.

### 5. Categorized Error Translation
Native `psycopg.OperationalError` and related driver exceptions are classified and translated into domain/application errors:
- **Authentication Failure** (`AuthenticationFailedError`): SQLSTATE `28P01` or authentication rejection. Tracks whether a saved password was used to provide a contextual hint (`support profile password set <NAME>`).
- **Host Resolution / Connection Refusal** (`HostUnreachableError`): SQLSTATE `08001`, `08006`, or socket errors indicating server offline or network unreachable.
- **Connection Timeout** (`ConnectionTimeoutError`): Exceeding the 10-second connect timeout.
- **Database Not Found** (`DatabaseNotFoundError`): SQLSTATE `3D000` (database does not exist).
- **SSL/TLS Verification Failure** (`SSLVerificationError`): Certificate validation or TLS handshake errors.
- **General Connection Error** (`GeneralConnectionError`): Unclassified operational connection issues.

## Consequences

- Direct, fast, and transparent connection testing without ORM or connection pool overhead.
- Passwords are kept entirely in memory and isolated from connection strings.
- Actionable, clean error output without leaking database secrets or raw stack traces.
