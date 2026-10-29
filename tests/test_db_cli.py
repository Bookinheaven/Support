"""Tests for database profile CLI commands (support db)."""

from pathlib import Path
import sqlite3
import pytest
from typer.testing import CliRunner
from support.cli.main import app

runner = CliRunner()


@pytest.fixture
def cli_test_sqlite_db(tmp_path: Path, monkeypatch) -> tuple[Path, str]:
    """Isolated environment for CLI database profile tests."""
    config_dir = tmp_path / ".support"
    monkeypatch.setenv("SUPPORT_CONFIG_DIR", str(config_dir))

    db_path = tmp_path / "cli_sample.db"
    conn = sqlite3.connect(db_path)
    cur = conn.cursor()
    cur.execute("CREATE TABLE projects (id INTEGER PRIMARY KEY, name TEXT NOT NULL UNIQUE);")
    cur.execute("CREATE TABLE tasks (id INTEGER PRIMARY KEY, project_id INTEGER NOT NULL REFERENCES projects(id), title TEXT);")
    conn.commit()
    conn.close()

    return db_path, str(db_path)


def test_cli_db_add_list_inspect_remove(cli_test_sqlite_db):
    db_file, db_str = cli_test_sqlite_db

    # 1. support db add
    add_res = runner.invoke(
        app,
        [
            "db",
            "add",
            "--name",
            "sample-sqlite",
            "--dialect",
            "sqlite",
            "--database",
            db_str,
        ],
    )
    assert add_res.exit_code == 0
    assert "Saved connection 'sample-sqlite'" in add_res.stdout

    # 2. support db list with live probing
    list_res = runner.invoke(app, ["db", "list"])
    assert list_res.exit_code == 0
    assert "sample-sqlite" in list_res.stdout
    assert "ONLINE" in list_res.stdout

    # 3. support db list --no-check
    list_nocheck = runner.invoke(app, ["db", "list", "--no-check"])
    assert list_nocheck.exit_code == 0
    assert "UNCHECKED" in list_nocheck.stdout

    # 4. support db test
    test_res = runner.invoke(app, ["db", "test", "sample-sqlite"])
    assert test_res.exit_code == 0
    assert "Connected successfully" in test_res.stdout

    # 5. support db inspect
    inspect_res = runner.invoke(app, ["db", "inspect", "sample-sqlite"])
    assert inspect_res.exit_code == 0
    assert "projects" in inspect_res.stdout
    assert "tasks" in inspect_res.stdout

    # 6. support db inspect --json
    inspect_json = runner.invoke(app, ["db", "inspect", "sample-sqlite", "--json"])
    assert inspect_json.exit_code == 0
    assert '"tables"' in inspect_json.stdout
    assert '"projects"' in inspect_json.stdout

    # 7. support db remove
    remove_res = runner.invoke(app, ["db", "remove", "sample-sqlite", "--yes"])
    assert remove_res.exit_code == 0
    assert "Removed connection profile 'sample-sqlite'" in remove_res.stdout

    # 8. confirm list is now empty
    list_empty = runner.invoke(app, ["db", "list"])
    assert "No saved database profiles" in list_empty.stdout


def test_cli_db_add_url_and_inferred_name(cli_test_sqlite_db):
    db_file, db_str = cli_test_sqlite_db

    # Add using URL with explicit name
    res = runner.invoke(
        app,
        [
            "db",
            "add",
            "--url",
            f"sqlite:///{db_str}",
            "--name",
            "url_profile",
        ],
    )
    assert res.exit_code == 0
    assert "Saved connection 'url_profile'" in res.stdout

    # Add using URL with inferred name (db_file stem is 'cli_sample')
    res_inferred = runner.invoke(
        app,
        [
            "db",
            "add",
            "--url",
            f"sqlite:///{db_str}",
        ],
    )
    assert res_inferred.exit_code == 0
    assert "Saved connection 'cli_sample'" in res_inferred.stdout

    # Verify both are ONLINE in db list
    list_res = runner.invoke(app, ["db", "list"])
    assert list_res.exit_code == 0
    assert "url_profile" in list_res.stdout
    assert "cli_sample" in list_res.stdout
    assert "ONLINE" in list_res.stdout


def test_cli_db_add_url_invalid(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("SUPPORT_CONFIG_DIR", str(tmp_path / ".support"))
    res = runner.invoke(app, ["db", "add", "--url", "invalid://host:123/db"])
    assert res.exit_code != 0
    assert "Invalid Connection URL" in res.stdout or "Unsupported dialect" in res.stdout


def test_cli_db_add_unreachable_fails_without_saving(tmp_path: Path, monkeypatch):
    """When connectivity test fails, profile should NOT be saved."""
    monkeypatch.setenv("SUPPORT_CONFIG_DIR", str(tmp_path / ".support"))
    res = runner.invoke(
        app,
        [
            "db",
            "add",
            "--url",
            "postgresql://user:pass@127.0.0.1:59998/testdb",
            "--name",
            "failing_pg",
        ],
    )
    assert res.exit_code != 0
    assert "Connection failed" in res.stdout
    assert "Connection not saved" in res.stdout

    # Confirm profile was not saved
    list_res = runner.invoke(app, ["db", "list", "--no-check"])
    assert "failing_pg" not in list_res.stdout


def test_cli_db_add_sqlite_nonexistent_does_not_create_file(tmp_path: Path, monkeypatch):
    """Registering a nonexistent SQLite db must fail and never create the file."""
    monkeypatch.setenv("SUPPORT_CONFIG_DIR", str(tmp_path / ".support"))
    non_existent = tmp_path / "never_created.db"
    assert not non_existent.exists()

    res = runner.invoke(
        app,
        [
            "db",
            "add",
            "--url",
            f"sqlite:///{non_existent}",
            "--name",
            "ghost_db",
        ],
    )
    assert res.exit_code != 0
    assert "Connection failed" in res.stdout
    assert not non_existent.exists()


def test_cli_db_offline_status_in_list(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("SUPPORT_CONFIG_DIR", str(tmp_path / ".support"))

    # Register an unreachable endpoint with --no-test so it saves
    res = runner.invoke(
        app,
        [
            "db",
            "add",
            "--url",
            "postgresql://user:pass@127.0.0.1:59998/testdb",
            "--name",
            "unreachable_db",
            "--no-test",
        ],
    )
    assert res.exit_code == 0
    assert "unverified" in res.stdout or "No connectivity check performed" in res.stdout

    # Running db list should detect TIMEOUT or OFFLINE
    list_res = runner.invoke(app, ["db", "list", "--timeout", "1"])
    assert list_res.exit_code == 0
    assert "unreachable_db" in list_res.stdout
    assert ("TIMEOUT" in list_res.stdout or "OFFLINE" in list_res.stdout)


def test_cli_db_test_nonexistent_profile(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("SUPPORT_CONFIG_DIR", str(tmp_path / ".support"))
    res = runner.invoke(app, ["db", "test", "non_existent_profile"])
    assert res.exit_code != 0
    assert "not found" in res.stdout
