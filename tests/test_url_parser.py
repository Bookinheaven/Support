"""Tests for database URL parsing, driver mapping, and profile name inference."""

import pytest
from support.core.dialect import Dialect
from support.core.exceptions import SupportError, UnsupportedDialectError
from support.db.url_parser import (
    parse_database_url,
    parse_driver_to_dialect,
    infer_profile_name,
)


def test_parse_driver_to_dialect():
    assert parse_driver_to_dialect("postgresql") == Dialect.POSTGRES
    assert parse_driver_to_dialect("postgresql+psycopg") == Dialect.POSTGRES
    assert parse_driver_to_dialect("postgres") == Dialect.POSTGRES
    assert parse_driver_to_dialect("mysql") == Dialect.MYSQL
    assert parse_driver_to_dialect("mysql+pymysql") == Dialect.MYSQL
    assert parse_driver_to_dialect("sqlite") == Dialect.SQLITE
    assert parse_driver_to_dialect("mssql+pymssql") == Dialect.MSSQL
    assert parse_driver_to_dialect("sqlserver") == Dialect.MSSQL

    with pytest.raises(UnsupportedDialectError):
        parse_driver_to_dialect("oracle")


def test_infer_profile_name():
    assert infer_profile_name(Dialect.POSTGRES, "athena") == "athena"
    assert infer_profile_name(Dialect.SQLITE, "d:/data/ecommerce.db") == "ecommerce"
    assert infer_profile_name(Dialect.SQLITE, ":memory:") == "sqlite_memory"

    # Handles existing duplicate names
    existing = {"athena", "athena-2"}
    assert infer_profile_name(Dialect.POSTGRES, "athena", existing_names=existing) == "athena-3"


def test_parse_database_url_postgresql():
    url = "postgresql://postgres:p%40ssw0rd@localhost:5433/athena_db?sslmode=prefer"
    profile, password = parse_database_url(url)

    assert profile.name == "athena_db"
    assert profile.dialect == Dialect.POSTGRES
    assert profile.username == "postgres"
    assert password == "p@ssw0rd"
    assert profile.host == "localhost"
    assert profile.port == 5433
    assert profile.database == "athena_db"
    assert profile.options == {"sslmode": "prefer"}


def test_parse_database_url_sqlite():
    url = "sqlite:///path/to/my_app.db"
    profile, password = parse_database_url(url)

    assert profile.name == "my_app"
    assert profile.dialect == Dialect.SQLITE
    assert profile.database == "path/to/my_app.db"
    assert password is None


def test_parse_database_url_explicit_name_override():
    url = "mysql://root:secret@127.0.0.1:3306/shop"
    profile, password = parse_database_url(url, name_override="production_mysql")

    assert profile.name == "production_mysql"
    assert profile.dialect == Dialect.MYSQL
    assert profile.username == "root"
    assert password == "secret"


def test_parse_database_url_invalid():
    with pytest.raises(SupportError):
        parse_database_url("not_a_valid_url")

    with pytest.raises(SupportError):
        parse_database_url("")
