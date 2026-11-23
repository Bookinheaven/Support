# 4. Multi-Database Domain Model, Canonical Schema Version 1, and Connector Dispatch

## Context

Support originally launched with PostgreSQL-specific domain models and database tables.
To support a wide developer audience, Support must support multiple popular relational and document databases:
- **PostgreSQL**
- **MySQL**
- **MariaDB**
- **SQLite**
- **Microsoft SQL Server**
- **MongoDB**

We must transition from a PostgreSQL-only structure to an extensible multi-database architecture while:
1. Retaining immutable typed validation without arbitrary unstructured dictionaries.
2. Avoiding premature generic driver frameworks, dynamic plugin discovery, or heavy ORM dependencies.
3. Preserving stable UUID profile identity and OS credential management.
4. Keeping the active PostgreSQL connector and test workflows functional.

## Decisions

### 1. Database-Specific Typed Connection Models
- Rather than forcing all databases into a single leaky abstraction (e.g. forcing `host`, `port`, or `ssl_mode` onto embedded SQLite), each database backend defines its own dedicated, immutable `@dataclass(frozen=True)`:
  - `PostgresConnection`: `database`, `username`, `ssl_mode`, `host`, `port`
  - `MySQLConnection`: `database`, `username`, `host`, `port`, `ssl_mode`
  - `MariaDBConnection`: `database`, `username`, `host`, `port`, `ssl_mode`
  - `SQLiteConnection`: `database_path`
  - `SQLServerConnection`: `database`, `username`, `host`, `port`, `encrypt`, `trust_server_certificate`
  - `MongoDBConnection`: `database`, `username` (optional), `host`, `port`, `auth_source`, `tls`
- Domain configuration uses a typed union:
  `ConnectionConfig = PostgresConnection | MySQLConnection | MariaDBConnection | SQLiteConnection | SQLServerConnection | MongoDBConnection`
- Each configuration strictly validates its own parameters in `__post_init__`.

### 2. Separation of Profile Identity and Connection Configuration
- Profile identity remains anchored by an immutable UUID (`profile.id`) and a validated unique alias (`profile.name`).
- Renaming a profile (`profile.rename()`) produces a new `DatabaseProfile` with the updated name while preserving its original UUID and typed connection configuration.
- OS credential storage keys database secrets strictly by profile UUID, ensuring that profile renames never desynchronize stored passwords.

### 3. Canonical Schema Version 1 with Serialized Typed Configuration
- The pre-release multi-database SQLite schema is established as the sole canonical schema version 1 (`PRAGMA user_version = 1`):
  ```sql
  CREATE TABLE profiles (
      id TEXT PRIMARY KEY NOT NULL,
      name TEXT UNIQUE NOT NULL,
      database_type TEXT NOT NULL,
      connection_config TEXT NOT NULL
  );
  ```
- `database_type` acts as a discriminator (`postgresql`, `mysql`, `mariadb`, `sqlite`, `sqlserver`, `mongodb`).
- `connection_config` contains JSON-serialized connection parameters specific to that database type.
- Serialized data strictly excludes secrets and passwords.
- Deserialization explicitly validates JSON structure, database type, and constructs the typed configuration.

### 4. Development Schema Handling
- Support is pre-release software. No pre-release migrations are maintained.
- Databases with obsolete development structures (such as the earlier PostgreSQL-only layout, or temporary version-2 development databases) are rejected safely without modification.
- Incompatible development databases must be backed up and recreated manually.
- Formal versioned migrations will be introduced when preserving released-user data becomes necessary.

### 5. Explicit Connector Dispatch
- `DatabaseConnector` port is generalized to accept `ConnectionConfig`.
- Infrastructure provides `ConnectorDispatcher`, which inspects the configuration type and routes it directly to its concrete driver adapter:
  - `PostgresConnection` routes to `PostgresConnector` (using Psycopg 3).
  - `SQLiteConnection` routes to `SQLiteConnector` (using Python's standard-library `sqlite3` in read-only mode).
  - Unimplemented connectors raise `UnsupportedDatabaseDriverError` indicating that connectivity testing for that specific database type is not yet supported.
- This explicit pattern avoids dynamic reflection, third-party plugin frameworks, and empty placeholder adapter classes while making future driver additions straightforward.

### 6. Credential Integration & Deferred Authentication Modes
- Username/password authentication continues to use OS-native keyrings for server databases.
- SQLite embedded databases do not require database passwords; password CLI commands explicitly disallow setting passwords on SQLite profiles.
- **Deferred Authentication Modes**: Advanced enterprise authentication methods—such as AWS IAM RDS authentication, GCP Cloud SQL IAM, Azure Active Directory / Entra ID, Kerberos/GSSAPI, client certificate authentication (mTLS), and MongoDB X.509—are explicitly deferred and will be modeled in future iterations rather than forced into standard password fields.

## Consequences

- Full architectural readiness for MySQL, MariaDB, SQLite, SQL Server, and MongoDB without altering the core profile workflow.
- Complete preservation of existing PostgreSQL functionality and test suites.
- Elimination of database-specific column bloat in SQLite storage.
