"""Tests for SQL DDL parsing and dialect detection."""

from pathlib import Path
from support.core.dialect import Dialect
from support.schema.sql_parser import SQLDDLParser, detect_dialect_from_sql
from support.schema.sources.sql_file import SQLFileSchemaSource


def test_detect_dialect_from_sql():
    pg_sql = "CREATE TABLE t (id SERIAL PRIMARY KEY, data JSONB);"
    assert detect_dialect_from_sql(pg_sql) == Dialect.POSTGRES

    mysql_sql = "CREATE TABLE t (id INT AUTO_INCREMENT PRIMARY KEY) ENGINE=InnoDB;"
    assert detect_dialect_from_sql(mysql_sql) == Dialect.MYSQL

    sqlite_sql = "CREATE TABLE t (id INTEGER PRIMARY KEY AUTOINCREMENT) WITHOUT ROWID;"
    assert detect_dialect_from_sql(sqlite_sql) == Dialect.SQLITE

    mssql_sql = "CREATE TABLE t (id INT IDENTITY(1,1), name NVARCHAR(MAX));\nGO"
    assert detect_dialect_from_sql(mssql_sql) == Dialect.MSSQL

    generic_sql = "CREATE TABLE t (id INT, val VARCHAR(50));"
    assert detect_dialect_from_sql(generic_sql) == Dialect.UNKNOWN


def test_parse_sql_tables_pk_fk(sample_postgres_sql: Path):
    parser = SQLDDLParser()
    sql_text = sample_postgres_sql.read_text(encoding="utf-8")
    schema, warnings = parser.parse_sql(sql_text, source_origin=str(sample_postgres_sql))

    assert schema.dialect == Dialect.POSTGRES
    assert set(schema.tables.keys()) == {"organizations", "users", "audit_logs"}

    # Test organizations table
    orgs = schema.tables["organizations"]
    assert orgs.primary_key == ["id"]
    id_col = orgs.get_column("id")
    assert id_col is not None
    assert id_col.is_primary_key is True
    name_col = orgs.get_column("name")
    assert name_col is not None
    assert name_col.nullable is False
    assert name_col.is_unique is True

    # Test users table and foreign key
    users = schema.tables["users"]
    assert users.primary_key == ["id"]
    assert len(users.foreign_keys) == 1
    fk = users.foreign_keys[0]
    assert fk.constrained_columns == ["org_id"]
    assert fk.referred_table.lower() == "organizations"
    assert fk.referred_columns == ["id"]
    assert fk.on_delete == "CASCADE"

    # Test audit_logs inline reference
    audit = schema.tables["audit_logs"]
    assert audit.primary_key == ["log_id"]
    assert len(audit.foreign_keys) == 1
    audit_fk = audit.foreign_keys[0]
    assert audit_fk.constrained_columns == ["user_id"]
    assert audit_fk.referred_table.lower() == "users"
    assert audit_fk.referred_columns == ["id"]


def test_sql_file_schema_source(sample_postgres_sql: Path):
    source = SQLFileSchemaSource(sample_postgres_sql)
    result = source.discover()

    assert not result.has_errors
    assert len(result.parsed_files) == 1
    assert result.discovered_files == [sample_postgres_sql]
    assert len(result.schema.tables) == 3
