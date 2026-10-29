"""Tests for SQLite database read-only inspection."""

from pathlib import Path
import pytest
from support.core.dialect import Dialect
from support.core.exceptions import InvalidPathError, DatabaseAccessError
from support.schema.sources.sqlite_db import SQLiteSchemaSource


def test_inspect_existing_sqlite_db(sample_sqlite_db: Path):
    source = SQLiteSchemaSource(sample_sqlite_db)
    result = source.discover()

    assert not result.has_errors
    assert result.schema.dialect == Dialect.SQLITE
    assert set(result.schema.tables.keys()) == {"departments", "employees"}

    # Validate departments table
    depts = result.schema.tables["departments"]
    assert depts.primary_key == ["dept_id"]
    dept_id = depts.get_column("dept_id")
    assert dept_id is not None
    assert dept_id.is_primary_key is True
    dept_name = depts.get_column("dept_name")
    assert dept_name is not None
    assert dept_name.nullable is False

    # Validate employees table
    emps = result.schema.tables["employees"]
    assert emps.primary_key == ["emp_id"]
    salary = emps.get_column("salary")
    assert salary is not None
    assert salary.default_value == "50000.0"

    # Foreign key check
    assert len(emps.foreign_keys) == 1
    fk = emps.foreign_keys[0]
    assert fk.constrained_columns == ["dept_id"]
    assert fk.referred_table.lower() == "departments"
    assert fk.referred_columns == ["dept_id"]
    assert fk.on_delete == "RESTRICT"

    # Unique index / constraint check
    assert len(emps.unique_constraints) >= 1
    uq_names = [uq.name for uq in emps.unique_constraints]
    assert "uq_emp_dept_name" in uq_names


def test_nonexistent_sqlite_file_does_not_create_file(tmp_path: Path):
    non_existent = tmp_path / "does_not_exist.db"
    assert not non_existent.exists()

    source = SQLiteSchemaSource(non_existent)
    with pytest.raises(InvalidPathError):
        source.discover()

    # Ensure file was NOT created automatically
    assert not non_existent.exists()


def test_invalid_sqlite_file_fails_gracefully(tmp_path: Path):
    fake_db = tmp_path / "corrupt.db"
    fake_db.write_text("This is not a SQLite database file!")

    source = SQLiteSchemaSource(fake_db)
    with pytest.raises(DatabaseAccessError):
        source.discover()
