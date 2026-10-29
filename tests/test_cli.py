"""Tests for CLI commands and error handling."""

import json
from pathlib import Path
from typer.testing import CliRunner
from support.cli.main import app

runner = CliRunner()


def test_cli_version():
    result = runner.invoke(app, ["version"])
    assert result.exit_code == 0
    assert "Support version" in result.stdout

    result_flag = runner.invoke(app, ["--version"])
    assert result_flag.exit_code == 0
    assert "Support version" in result_flag.stdout


def test_cli_scan_sample_project(sample_project_dir: Path):
    result = runner.invoke(app, ["scan", str(sample_project_dir)])
    assert result.exit_code == 0
    assert "Support — Database Schema Discovery" in result.stdout
    assert "users" in result.stdout
    assert "organizations" in result.stdout
    assert "products" in result.stdout


def test_cli_scan_with_explicit_dialect(sample_project_dir: Path):
    result = runner.invoke(app, ["scan", str(sample_project_dir), "--dialect", "postgres"])
    assert result.exit_code == 0
    assert "PostgreSQL (postgres)" in result.stdout


def test_cli_scan_sqlite_database_file(sample_sqlite_db: Path):
    result = runner.invoke(app, ["scan", str(sample_sqlite_db)])
    assert result.exit_code == 0
    assert "SQLite (sqlite)" in result.stdout
    assert "departments" in result.stdout
    assert "employees" in result.stdout


def test_cli_inspect_sqlite_command(sample_sqlite_db: Path):
    result = runner.invoke(app, ["inspect-sqlite", str(sample_sqlite_db)])
    assert result.exit_code == 0
    assert "departments" in result.stdout
    assert "employees" in result.stdout


def test_cli_invalid_path():
    result = runner.invoke(app, ["scan", "non_existent_folder_abc_123"])
    assert result.exit_code != 0
    assert "Invalid Path" in result.stdout or "does not exist" in result.stdout


def test_cli_malformed_sql_file(tmp_path: Path):
    bad_dir = tmp_path / "bad_project"
    bad_dir.mkdir()
    bad_file = bad_dir / "broken.sql"
    bad_file.write_text("CREATE TABLE broken (id INT NOT VALID SQL ;;;")

    # Scanner should run, discover file, and report the parsing failure gracefully in the output
    result = runner.invoke(app, ["scan", str(bad_dir)])
    assert "Unsupported / Failed Files" in result.stdout or "broken.sql" in result.stdout


def test_cli_json_output(sample_project_dir: Path):
    result = runner.invoke(app, ["scan", str(sample_project_dir), "--json"])
    assert result.exit_code == 0
    parsed = json.loads(result.stdout)
    assert "tables" in parsed
    assert "users" in parsed["tables"]
