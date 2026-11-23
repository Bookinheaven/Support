"""Integration tests for SQLite schema introspection."""

import sqlite3
from pathlib import Path
import pytest

from support.application.errors import ObjectNotFoundError
from support.domain.profiles import SQLiteConnection
from support.domain.schema import TableType
from support.infrastructure.sqlite_connector import SQLiteConnector


def _setup_test_db(db_path: Path) -> None:
    conn = sqlite3.connect(str(db_path))
    with conn:
        conn.execute(
            """
            CREATE TABLE authors (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                email TEXT UNIQUE,
                bio TEXT DEFAULT 'No bio provided'
            );
            """
        )
        conn.execute(
            """
            CREATE TABLE books (
                id INTEGER PRIMARY KEY,
                author_id INTEGER NOT NULL,
                title TEXT NOT NULL,
                price REAL NOT NULL,
                FOREIGN KEY (author_id) REFERENCES authors (id) ON DELETE CASCADE
            );
            """
        )
        conn.execute("CREATE INDEX idx_books_title ON books (title);")
        conn.execute("CREATE VIEW active_books AS SELECT id, title FROM books WHERE price > 0;")
    conn.close()


def test_sqlite_introspect_schema(tmp_path: Path) -> None:
    db_file = tmp_path / "library.db"
    _setup_test_db(db_file)

    connector = SQLiteConnector()
    conn_config = SQLiteConnection(database_path=str(db_file))

    schema = connector.introspect_schema(conn_config)

    assert schema.database_name == "library.db"
    assert schema.engine == "SQLite"
    assert len(schema.tables) == 3  # authors, books, active_books

    # Check authors table
    authors = schema.get_table("authors")
    assert authors is not None
    assert authors.name == "authors"
    assert authors.table_type == TableType.TABLE
    assert len(authors.columns) == 4
    col_names = [c.name for c in authors.columns]
    assert col_names == ["id", "name", "email", "bio"]

    assert authors.primary_key is not None
    assert authors.primary_key.columns == ("id",)

    # Check books table
    books = schema.get_table("books")
    assert books is not None
    assert len(books.foreign_keys) == 1
    fk = books.foreign_keys[0]
    assert fk.source_table == "books"
    assert fk.source_columns == ("author_id",)
    assert fk.target_table == "authors"
    assert fk.target_columns == ("id",)
    assert fk.on_delete == "CASCADE"

    # Check active_books view
    view = schema.get_table("active_books")
    assert view is not None
    assert view.table_type == TableType.VIEW
    assert len(view.columns) == 2

    # Verify overall relationships in schema
    assert len(schema.relationships) == 1
    assert schema.relationships[0] == fk


def test_sqlite_introspect_table(tmp_path: Path) -> None:
    db_file = tmp_path / "library.db"
    _setup_test_db(db_file)

    connector = SQLiteConnector()
    conn_config = SQLiteConnection(database_path=str(db_file))

    table_meta = connector.introspect_table(conn_config, "books")
    assert table_meta.name == "books"
    assert table_meta.table_type == TableType.TABLE
    assert len(table_meta.columns) == 4
    assert len(table_meta.foreign_keys) == 1
    assert any(idx.name == "idx_books_title" for idx in table_meta.indexes)


def test_sqlite_introspect_table_not_found(tmp_path: Path) -> None:
    db_file = tmp_path / "library.db"
    _setup_test_db(db_file)

    connector = SQLiteConnector()
    conn_config = SQLiteConnection(database_path=str(db_file))

    with pytest.raises(ObjectNotFoundError):
        connector.introspect_table(conn_config, "nonexistent_table")


def test_sqlite_internal_objects_excluded(tmp_path: Path) -> None:
    db_file = tmp_path / "library.db"
    _setup_test_db(db_file)

    connector = SQLiteConnector()
    conn_config = SQLiteConnection(database_path=str(db_file))

    schema = connector.introspect_schema(conn_config)
    names = [t.name for t in schema.tables]
    # sqlite_sequence is created for AUTOINCREMENT
    assert "sqlite_sequence" not in names
    assert all(not name.startswith("sqlite_") for name in names)


def test_sqlite_advanced_features_introspection(tmp_path: Path) -> None:
    db_file = tmp_path / "advanced.db"
    conn = sqlite3.connect(str(db_file))
    with conn:
        conn.execute(
            """
            CREATE TABLE parent_orgs (
                tenant_id INTEGER NOT NULL,
                org_code TEXT NOT NULL,
                org_name TEXT NOT NULL,
                PRIMARY KEY (tenant_id, org_code)
            );
            """
        )
        conn.execute(
            """
            CREATE TABLE employees (
                tenant_id INTEGER NOT NULL,
                org_code TEXT NOT NULL,
                emp_id INTEGER NOT NULL,
                first_name TEXT NOT NULL,
                last_name TEXT NOT NULL,
                salary REAL CHECK (salary > 0),
                bonus REAL DEFAULT 0,
                full_name TEXT GENERATED ALWAYS AS (first_name || ' ' || last_name) VIRTUAL,
                total_comp REAL GENERATED ALWAYS AS (salary + bonus) STORED,
                PRIMARY KEY (tenant_id, org_code, emp_id),
                FOREIGN KEY (tenant_id, org_code) REFERENCES parent_orgs (tenant_id, org_code) ON DELETE CASCADE,
                CONSTRAINT chk_total CHECK ((salary + bonus > 0) AND (salary < 1000000))
            );
            """
        )
        # Partial index
        conn.execute("CREATE INDEX idx_emp_high_salary ON employees (salary DESC) WHERE salary > 100000;")
        # Functional expression index
        conn.execute("CREATE INDEX idx_emp_lower_last ON employees (lower(last_name) ASC);")
    conn.close()

    connector = SQLiteConnector()
    conn_config = SQLiteConnection(database_path=str(db_file))

    schema = connector.introspect_schema(conn_config)
    emp = schema.get_table("employees")
    assert emp is not None

    # Check composite primary key
    assert emp.primary_key is not None
    assert emp.primary_key.columns == ("tenant_id", "org_code", "emp_id")

    # Check composite foreign key
    assert len(emp.foreign_keys) == 1
    fk = emp.foreign_keys[0]
    assert fk.source_columns == ("tenant_id", "org_code")
    assert fk.target_table == "parent_orgs"
    assert fk.target_columns == ("tenant_id", "org_code")
    assert fk.on_delete == "CASCADE"

    # Check generated columns
    col_names = [c.name for c in emp.columns]
    assert "full_name" in col_names
    assert "total_comp" in col_names

    # Check check constraints
    check_clauses = [c.check_clause for c in emp.check_constraints]
    assert any("salary > 0" in cl for cl in check_clauses)
    assert any("salary + bonus > 0" in cl and "salary < 1000000" in cl for cl in check_clauses)

    # Check indexes
    idx_high = next(i for i in emp.indexes if i.name == "idx_emp_high_salary")
    assert idx_high.column_orders == ("DESC",)
    assert idx_high.filter_condition == "salary > 100000"

    idx_expr = next(i for i in emp.indexes if i.name == "idx_emp_lower_last")
    assert "lower(last_name)" in idx_expr.columns
    assert idx_expr.column_orders == ("ASC",)

    # Test introspect_table with quoting
    tbl_quoted = connector.introspect_table(conn_config, '"employees"')
    assert tbl_quoted.name == "employees"
