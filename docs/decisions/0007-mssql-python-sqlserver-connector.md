# 7. Microsoft SQL Server Connection Testing via mssql-python

## Context

Support requires connection testing (`support profile test <NAME>`) for Microsoft SQL Server profiles alongside PostgreSQL, SQLite, MySQL, and MariaDB.

SQL Server is an enterprise relational database system with distinct connection paradigms:
1. Microsoft has released an official, modern Python driver: `mssql-python`.
2. Traditional Python connectors for SQL Server (`pyodbc`, `pymssql`) suffer from major drawbacks:
   - `pyodbc` requires installing external system ODBC managers (`unixODBC` on Linux, `iODBC`/`unixODBC` on macOS) and separately downloading/installing Microsoft ODBC Driver for SQL Server via vendor package repositories.
   - `pymssql` is based on FreeTDS, which is not an official Microsoft driver, lags behind modern SQL Server TDS features (such as modern TLS encryption and token-based auth), and has intermittent maintenance cycles.
3. `mssql-python` packages the Direct Driver for C/C++ (DDBC) and Rust TDS components directly within platform wheels, eliminating external ODBC manager installation.
4. `mssql-python` enables connection pooling by default, whereas Support operates short-lived CLI database commands where persistent pooling is inappropriate.
5. Passwords and credentials must be protected against leaks in process arguments, connection strings, error messages, and logs.
6. Encryption and server certificate validation must strictly adhere to domain configurations without silently weakening TLS guarantees.

## Decision

We adopt **`mssql-python`** (`mssql-python>=1.18.0,<2`) and implement `SQLServerConnector` in `support.infrastructure.sqlserver`.

### 1. Driver Selection: mssql-python vs. pyodbc / pymssql
- **`mssql-python`**: Microsoft's official DB-API 2.0 driver for SQL Server and Azure SQL.
  - Automatically installs companion packages (`mssql-python-odbc` and `mssql-python-rs`) containing the bundled native driver runtime.
  - Eliminates the requirement for developers to configure `/etc/odbcinst.ini` or install external ODBC driver managers manually.
  - Avoids `pyodbc` (which requires manual driver manager setup and external driver downloads) and `pymssql` (community FreeTDS wrapper).
  - Avoids heavy ORMs or generic abstraction layers (SQLAlchemy).

### 2. Native Platform Prerequisites and Compatibility
While `mssql-python` bundles its Direct Database Connectivity (DDBC) ODBC provider inside wheels, operating-system support and runtime dependencies are structured as follows:

#### Officially Documented Operating-System Support (Microsoft)
- **Windows**: Windows 11 (x64, ARM64); Windows Server 2019, 2022, and 2025 (x64).
- **macOS**: macOS 15 and later (Apple Silicon `arm64`, Intel `x86_64`).
- **Linux**: Ubuntu (22.04, 24.04, 26.04; x64, ARM64), Debian (11, 12, 13; x64, ARM64), Red Hat Enterprise Linux / RHEL (8, 9; x64, ARM64), and Alpine Linux (x64, ARM64).

#### Available Package Wheels
- Published on PyPI as prebuilt platform wheels:
  - Windows: `win_amd64`, `win_arm64`.
  - macOS: `macosx_15_0_universal2` (combining Apple Silicon and Intel).
  - Linux: `manylinux_2_28` and `manylinux_2_34` (x86_64, aarch64), and `musllinux_1_2` (x86_64, aarch64).

#### Additional Runtime Library Requirements
- **Windows**: Zero external dependencies; `pip install mssql-python` provides the entire bundled driver runtime.
- **macOS**: Requires system OpenSSL (e.g. `brew install openssl`).
- **Linux**: Requires system OpenSSL as well as dynamic loading and Kerberos system packages:
  - Debian / Ubuntu: `libltdl7`, `libkrb5-3`, `libgssapi-krb5-2`.
  - RHEL / CentOS / Rocky / AlmaLinux / Fedora: `libtool-ltdl`, `krb5-libs`.
  - Alpine: `libltdl`, `krb5-libs`, `libstdc++`.

### 3. Connection Pooling Policy
- `mssql-python` enables connection pooling by default across processes.
- Because Support is a terminal workspace focused on deterministic, short-lived commands, persistent connection pools can hold idle sockets or cause unexpected state retention across CLI executions.
- `SQLServerConnector` explicitly calls `mssql_python.pooling(enabled=False)` before establishing connections, disabling driver pooling at the process level.

### 4. Connection Lifecycle & Timeouts
- **Separation of Timeouts**:
  - Login / connection attempt timeout: bounded to 10 seconds via `timeout=10` in `mssql_python.connect()`.
  - Statement / query execution timeout: bounded to 10 seconds via `conn.timeout = 10` on the active connection.
- **Autocommit**: Opened with `autocommit=True` so that the read-only test verification (`SELECT 1`) leaves no pending transactions or locks.
- **Deterministic Cleanup**: Connections and cursors are closed immediately in `finally` blocks.

### 5. Parameter Mapping & Secret Protection
- Keyword arguments are passed directly to `mssql_python.connect()`:
  - `Server`: formatted as `"host,port"` per driver documentation.
  - `Database`: target database name.
  - `UID`: username.
  - `PWD`: password passed safely as an individual parameter (omitted when None).
  - `Encrypt`: `"yes"` if `connection.encrypt` else `"no"`.
  - `TrustServerCertificate`: `"yes"` if `connection.trust_server_certificate` else `"no"`.
- Password sanitization: Any exception message containing the raw password is automatically sanitized before formatting error output.

### 6. Error Translation
Driver exceptions are translated into the application's `DatabaseConnectionError` hierarchy based on SQLSTATE, `driver_error`, and `ddbc_error`:
- **Authentication Failure** (`AuthenticationFailedError`): SQLSTATE `28000`, driver error `"Invalid authorization specification"`, or SQL Server error 18456 ("Login failed for user").
- **Database Not Found** (`DatabaseNotFoundError`): SQLSTATE `3D000`, driver error `"Invalid catalog name"`, or SQL Server error 4060 ("Cannot open database requested by the login").
- **Connection Timeout** (`ConnectionTimeoutError`): SQLSTATE `HYT00` or `HYT01`, driver error `"Timeout expired"` / `"Connection timeout expired"`.
- **TLS / SSL Verification Failure** (`SSLVerificationError`): Errors citing SSL Provider, certificate chain validation, or untrusted authority.
- **Host / Network Unreachable** (`HostUnreachableError`): SQLSTATE `08001`, `08S01`, or TCP provider connection refusal.
- **General Connection Error** (`GeneralConnectionError`): Unclassified operational errors without leaking secrets.

### 7. Authentication Scope & Deferred Modes
- **Supported in Current Scope**: Standard SQL Server SQL Authentication (username and password) stored securely via OS-native credential management (`keyring`) or entered via hidden interactive terminal prompts.
- **Deferred Authentication Methods**:
  - Microsoft Entra ID (formerly Azure Active Directory): Interactive, Managed Identity, Service Principal, and token provider hooks.
  - Windows Integrated Authentication (NTLM / Kerberos).
  - Client-certificate mTLS.

## Consequences

- Direct, reliable SQL Server connectivity testing via Microsoft's official driver.
- Bundled driver components avoid external ODBC package installation on supported platforms.
- Clear separation between connection testing, credential management, and future query/schema inspection workflows.
