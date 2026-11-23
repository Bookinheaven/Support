# 8. MongoDB Connection Testing via PyMongo

## Context

Support requires connection testing (`support profile test <NAME>`) for MongoDB profiles alongside existing PostgreSQL, SQLite, MySQL, MariaDB, and Microsoft SQL Server connectors.

MongoDB is a document-oriented NoSQL database system with distinct operational paradigms:
1. **Connection String Formats**:
   - Standard `mongodb://host[:port][,...hostN[:portN]][/database][?options]`.
   - DNS Seedlist / SRV connection strings `mongodb+srv://cluster.domain[/database][?options]`, common in managed clouds like MongoDB Atlas.
2. **Synchronous CLI Operations**:
   - Support is a terminal-based CLI built on Typer. Asynchronous drivers (such as Motor) require managing an event loop, async tasks, and cross-thread coordination for short-lived commands.
3. **Connectivity Verification**:
   - MongoDB does not use SQL (`SELECT 1`). Instead, database reachability and authentication are verified via the administrative database command `ping` (`client.admin.command("ping")`).
4. **Secret Protection**:
   - Connection strings frequently leak plain-text credentials in logs or shell history when user info is embedded (`mongodb://user:pass@host`).
   - Passwords must be handled exclusively through Support's OS-native credential store (`keyring`), never persisted in SQLite metadata, and never leaked in exception messages.

## Decision

We adopt **`pymongo`** (`pymongo>=4.10,<5`) and implement `MongoDBConnector` in `support.infrastructure.mongodb`.

### 1. Driver Selection: PyMongo vs. Motor / MongoEngine
- **`pymongo`**: The official, synchronously maintained driver developed and supported by MongoDB, Inc.
  - Native fit for Support's synchronous CLI architecture.
  - Lightweight, fast connection lifecycle without background event-loop overhead.
  - Supports standard and SRV URIs, replica sets, TLS, and modern SCRAM-SHA-1 / SCRAM-SHA-256 authentication.
- **Motor**: An async wrapper around PyMongo for asyncio/Tornado; adds unwanted async event loop complexity to simple synchronous CLI test commands.
- **MongoEngine / Beanie**: Heavy ODM layers inappropriate for non-invasive connection testing and schema-agnostic workspace tools.

### 2. Connection Model & URI Representation
- `MongoDBConnection` domain model:
  - `uri`: A clean deployment URI (`mongodb://` or `mongodb+srv://`) containing cluster addresses and non-secret query options (e.g. `retryWrites=true`, `authSource=admin`).
  - `database`: Target database name (defaults to `"admin"` or extracted from the URI path).
  - `username`: Optional login username.
- **Target Database versus Authentication Database Resolution**:
  - Target database (`database`): The target database for operations, resolved from the URI path (e.g. `/analytics`) or explicit `--database` option (defaulting to `"admin"`).
  - Authentication database (`authSource`): The database against which user credentials are authenticated:
    1. If `authSource` is explicitly specified in the URI query (e.g. `/analytics?authSource=admin`), credentials are authenticated against that database (`admin`).
    2. If `authSource` is omitted but a database path exists in the URI (e.g. `/analytics`), credentials default to that path database (`analytics`) per MongoDB URI specifications.
    3. If neither `authSource` nor a database path exists in the URI (e.g. `mongodb://localhost:27017`), credentials default to the administrative database (`admin`).
  - The target database does not automatically override the authentication database; explicit URI options are preserved verbatim.
- **Security Invariant**:
  - Embedded credentials in the URI (`mongodb://user:pass@...`) are strictly prohibited and rejected during profile validation.
  - Secret-bearing query options (`tlsCertificateKeyFilePassword`, `sslCertificateKeyFilePassword`, `proxyPassword`, `authMechanismProperties`) are rejected during domain validation.
  - Ordinary non-secret connection parameters (`authSource`, `tls`, `ssl`, `replicaSet`, `retryWrites`, `w`, `readPreference`, `appName`) are permitted and preserved.
  - Passwords are provided solely via the OS-native keyring or interactive one-time input during `support profile test`.

### 3. Strict Profile Persistence & Obsolete Format Policy
- `profile_config_codec.py` strictly decodes canonical `MongoDBConnection` records containing `"uri"`.
- Legacy pre-release records containing raw `host`, `port`, `tls`, and `auth_source` are rejected with `ProfileStorageError`, informing the developer that the profile is obsolete and must be recreated.
- Schema version remains version 1 without migrations.

### 4. Connection Verification & Timeouts
- **Short-Lived Connection Verification**:
  - `MongoDBConnector.test_connection()` instantiates a short-lived `MongoClient`.
  - Sets bounded 10-second limits: `serverSelectionTimeoutMS=10000`, `connectTimeoutMS=10000`, `socketTimeoutMS=10000`, `timeoutMS=10000`.
  - Executes `client.admin.command("ping")` as the single, decisive verification command.
  - Guarantees:
    - Server selection and cluster connectivity are successful.
    - Authentication is successful when credentials are configured and authentication is required.
    - The configured target database is recorded and returned as profile metadata.
  - Non-guarantees:
    - Target database existence and read/write permissions are NOT verified by this connection test.
    - No collection enumeration, CRUD operations, or permission probes are executed.
  - Does not issue `buildInfo`: connectivity verification relies exclusively on `ping`. Server version remains unset (`None`) in connection test results.
  - Guaranteed `client.close()` in a `finally` block on both success and failure paths.

### 5. Error Classification & Secret Sanitization
- **Exception Inheritance & Handler Ordering**:
  - PyMongo's `NetworkTimeout` and `ServerSelectionTimeoutError` both inherit from `AutoReconnect`, which inherits from `ConnectionFailure`.
  - Handlers are explicitly ordered from most specific to generic:
    1. `ServerSelectionTimeoutError` → `HostUnreachableError`.
    2. `NetworkTimeout` → `ConnectionTimeoutError`.
    3. `ConnectionFailure` (other `AutoReconnect` / socket connection drops) → `HostUnreachableError`.
    4. `OperationFailure` with code 18 (`AuthenticationFailed`) → `AuthenticationFailedError`.
    5. `OperationFailure` with code 13 (`Unauthorized`) → `GeneralConnectionError` (preserving the distinction between credential failure and command authorization denial).
    6. `ConfigurationError` (SRV DNS failure) → `HostUnreachableError`.
    7. `ConfigurationError` (Invalid URI options) → `GeneralConnectionError`.
    8. Missing PyMongo package → `UnsupportedDatabaseDriverError` with actionable `pip install "support-db[mongodb]"`.
- All exception messages are sanitized to redact passwords and strip full connection strings before presentation.

### 6. Deferred Capabilities
- Collection listing, document sampling, and CRUD queries are out of scope and deferred.
- Advanced authentication mechanisms (e.g. Kerberos / GSSAPI, AWS IAM, LDAP, X.509 client certificates) are deferred.
