"""Pytest fixtures for Support tests."""

import sqlite3
from pathlib import Path
import pytest


@pytest.fixture
def sample_postgres_sql(tmp_path: Path) -> Path:
    """Fixture providing a sample PostgreSQL DDL migration file."""
    sql_file = tmp_path / "001_create_users.sql"
    content = """
    -- Initial schema
    CREATE TABLE organizations (
        id SERIAL PRIMARY KEY,
        name VARCHAR(100) NOT NULL UNIQUE,
        created_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP
    );

    CREATE TABLE users (
        id SERIAL PRIMARY KEY,
        org_id INT NOT NULL,
        email VARCHAR(255) NOT NULL UNIQUE,
        full_name VARCHAR(100),
        is_active BOOLEAN DEFAULT TRUE,
        CONSTRAINT fk_user_org FOREIGN KEY (org_id)
            REFERENCES organizations(id) ON DELETE CASCADE
    );

    CREATE TABLE audit_logs (
        log_id BIGSERIAL PRIMARY KEY,
        user_id INT REFERENCES users(id),
        action VARCHAR(50) NOT NULL,
        payload JSONB
    );
    """
    sql_file.write_text(content, encoding="utf-8")
    return sql_file


@pytest.fixture
def sample_sqlite_db(tmp_path: Path) -> Path:
    """Fixture creating an authentic SQLite database with tables, FKs, and constraints."""
    db_file = tmp_path / "app.sqlite"
    conn = sqlite3.connect(db_file)
    cur = conn.cursor()

    cur.execute("""
    CREATE TABLE departments (
        dept_id INTEGER PRIMARY KEY AUTOINCREMENT,
        dept_name TEXT NOT NULL UNIQUE
    );
    """)

    cur.execute("""
    CREATE TABLE employees (
        emp_id INTEGER PRIMARY KEY,
        dept_id INTEGER NOT NULL,
        name TEXT NOT NULL,
        salary REAL DEFAULT 50000.0,
        FOREIGN KEY (dept_id) REFERENCES departments(dept_id) ON DELETE RESTRICT
    );
    """)

    cur.execute("""
    CREATE UNIQUE INDEX uq_emp_dept_name ON employees (dept_id, name);
    """)

    conn.commit()
    conn.close()
    return db_file


@pytest.fixture
def sample_project_dir(tmp_path: Path, sample_postgres_sql: Path, sample_sqlite_db: Path) -> Path:
    """Fixture creating a realistic project layout."""
    proj = tmp_path / "my_project"
    proj.mkdir()

    # Valid migrations folder
    migrations = proj / "db" / "migrations"
    migrations.mkdir(parents=True)
    (migrations / "001_users.sql").write_text(sample_postgres_sql.read_text())

    # Add a MySQL migration
    mysql_file = migrations / "002_inventory.sql"
    mysql_file.write_text("""
    CREATE TABLE products (
        product_id INT AUTO_INCREMENT PRIMARY KEY,
        sku VARCHAR(64) NOT NULL UNIQUE,
        stock INT DEFAULT 0
    ) ENGINE=InnoDB;
    """)

    # Irrelevant folders that scanner must ignore
    ignored_git = proj / ".git"
    ignored_git.mkdir()
    (ignored_git / "config.sql").write_text("CREATE TABLE should_be_ignored (id INT);")

    ignored_venv = proj / ".venv" / "lib"
    ignored_venv.mkdir(parents=True)
    (ignored_venv / "site.sql").write_text("CREATE TABLE venv_ignored (id INT);")

    ignored_nm = proj / "node_modules" / "pkg"
    ignored_nm.mkdir(parents=True)
    (ignored_nm / "cache.sql").write_text("CREATE TABLE nm_ignored (id INT);")

    return proj
