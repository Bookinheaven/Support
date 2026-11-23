# 6. Direct MySQL and MariaDB Connection Testing via PyMySQL

## Context

Support requires connection testing (`support profile test <NAME>`) for MySQL and MariaDB database profiles alongside PostgreSQL and SQLite.

MySQL and MariaDB are separate server forks that share the underlying MySQL client-server wire protocol.
We must select a client driver strategy and architecture while ensuring that:
1. No native C compilation or external C client libraries (such as `libmysqlclient`, `mariadb-connector-c`, or ODBC) are required across supported platforms (Windows, macOS, Linux).
2. We avoid duplicate connector implementations for MySQL and MariaDB.
3. Passwords and credentials are never leaked in process arguments, connection strings, error messages, or logs.
4. SSL/TLS semantics accurately reflect domain configurations without silently weakening certificate verification.
5. Safe defaults are strictly enforced (e.g. disabling local file loading, preventing arbitrary client option file evaluation).

## Decision

We adopt **PyMySQL** (`PyMySQL[rsa]>=1.2.4,<2`) and introduce a single shared adapter, `MySQLCompatibleConnector`, in `support.infrastructure.mysql_connector`.

### 1. Pure-Python Driver Selection
- PyMySQL is a pure-Python implementation of the MySQL wire protocol.
- It requires no C compiler toolchains, binary wheels, or native database client libraries, making cross-platform installation and maintenance seamless across Linux, macOS, and Windows.
- The `rsa` extra (`cryptography`) is included to support modern MySQL authentication plugins such as `caching_sha2_password` and `sha256_password`.
- Avoided alternatives:
  - `mysqlclient`: Requires native C headers and platform-specific compilation.
  - `mariadb`: Requires native MariaDB Connector/C libraries.
  - SQLAlchemy / ODBC: Introduces heavy ORM, connection pooling, and dialect overhead unnecessary for bounded connection testing.

### 2. Single Shared Adapter Design
- MySQL and MariaDB share the standard wire protocol handshake, packet structure, and error framing.
- Rather than duplicating connection logic into separate adapters, `MySQLCompatibleConnector` implements the `DatabaseConnector` port and handles both `MySQLConnection` and `MariaDBConnection` domain configurations.
- The `ConnectorDispatcher` routes both connection types directly to `MySQLCompatibleConnector`.

### 3. Safe Defaults & Finite Bounded Timeouts
- Parameterized individual keyword arguments are passed directly to `pymysql.connect()`. Connection strings or URIs containing passwords are never constructed.
- **Security Defaults**:
  - `local_infile=False`: Explicitly disables local file inclusion to prevent malicious server reads of client files.
  - `read_default_file=None` & `read_default_group=None`: Prevents implicit loading of arbitrary `.my.cnf` option files from the host environment.
  - `autocommit=False`: Prevents altering server transaction states during connection testing.
- **Finite Timeouts**: Enforces 10.0-second timeouts for connection establishment (`connect_timeout`), socket reads (`read_timeout`), and socket writes (`write_timeout`).

### 4. TLS / SSL Semantic Mapping
Each domain configuration defines distinct SSL mode vocabularies, which are mapped to consistent cryptographic outcomes via Python's standard-library `ssl` module:

| Semantic Outcome | MySQL Mode (`ssl_mode`) | MariaDB Mode (`ssl_mode`) | Driver Configuration |
|---|---|---|---|
| **Disabled** | `disabled` | `disabled` | `ssl_disabled=True, ssl=None` |
| **Preferred** | `preferred` | `prefer` | `ssl_disabled=False, ssl=None` (attempts TLS; falls back if unencrypted) |
| **Required** | `required` | `require` | `SSLContext(PROTOCOL_TLS_CLIENT)` with `verify_mode=CERT_NONE, check_hostname=False` |
| **Verify CA** | `verify_ca` | `verify-ca` | `create_default_context()` with `verify_mode=CERT_REQUIRED, check_hostname=False` |
| **Verify Identity / Full** | `verify_identity` | `verify-full` | `create_default_context()` with `verify_mode=CERT_REQUIRED, check_hostname=True` |

System trust roots are used when verifying certificates. Custom CA file paths, client certificates, and mutual TLS (mTLS) are not currently exposed in domain configurations and are deferred.

### 5. Verification Query & Server Version
- Executes `SELECT 1` and verifies the result is exactly `1`.
- Extracts the remote server engine version string via PyMySQL's public API `conn.get_server_info()` without executing supplementary queries.
- Connections and cursors are deterministically closed in `finally` blocks immediately upon test completion.

### 6. Categorized Error Translation
PyMySQL errors are inspected via vendor error codes (`pymysql.constants.ER`), client codes (`pymysql.constants.CR`), SQLSTATE, and underlying socket exceptions:
- **Authentication Failure** (`AuthenticationFailedError`): Vendor code 1045 (`ER.ACCESS_DENIED_ERROR`) or SQLSTATE `28000`.
- **Database Access Denied** (`GeneralConnectionError`): Vendor code 1044 (`ER.DBACCESS_DENIED_ERROR`). The user authenticated successfully but lacks access privileges to the target database; password remediation is not suggested.
- **Database Not Found** (`DatabaseNotFoundError`): Vendor code 1049 (`ER.BAD_DB_ERROR`). Generic SQLSTATE `42000` is not mapped to missing databases.
- **Host Resolution / Connection Refusal** (`HostUnreachableError`): `CR.CR_UNKNOWN_HOST` (2005), `CR.CR_CONN_HOST_ERROR` (2003) with `ConnectionRefusedError` or socket resolution errors.
- **Connection Timeout** (`ConnectionTimeoutError`): `socket.timeout` or socket timeout errors exceeding 10 seconds.
- **SSL/TLS Verification Failure** (`SSLVerificationError`): `CR.CR_SSL_CONNECTION_ERROR` (2026), `ssl.SSLError`, or certificate validation errors.
- **General Connection Error** (`GeneralConnectionError`): Unclassified operational errors without leaking connection secrets.

### 7. Deferred Authentication Methods
Support provides standard username/password authentication. The following advanced enterprise authentication mechanisms are deferred to future milestones:
- MariaDB `ed25519` plugin.
- Kerberos / GSSAPI.
- LDAP / PAM authentication.
- Cloud IAM database authentication (AWS IAM RDS, GCP Cloud SQL IAM, Azure Entra ID).
- Client-certificate (mTLS) authentication.

## Consequences

- Direct, reliable connection testing for both MySQL and MariaDB using a single pure-Python codebase.
- Zero native compiler dependencies across Windows, macOS, and Linux.
- Strict protection of database passwords and isolation from shell arguments or error messages.
