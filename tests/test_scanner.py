"""Tests for project directory scanner."""

from pathlib import Path
from support.core.config import AppConfig
from support.project.scanner import ProjectScanner


def test_scanner_ignores_common_directories(sample_project_dir: Path):
    scanner = ProjectScanner()
    discovered = scanner.scan(sample_project_dir)

    # Should find migrations
    found_filenames = {p.name for p in discovered.sql_files}
    assert "001_users.sql" in found_filenames
    assert "002_inventory.sql" in found_filenames

    # Should NOT find files in .git, .venv, node_modules
    assert "config.sql" not in found_filenames
    assert "site.sql" not in found_filenames
    assert "cache.sql" not in found_filenames


def test_scanner_skips_oversized_files(tmp_path: Path):
    config = AppConfig(max_file_size_bytes=100)
    scanner = ProjectScanner(config=config)

    huge_file = tmp_path / "huge.sql"
    huge_file.write_text("CREATE TABLE huge (" + "col INT, " * 50 + "id INT);")
    assert huge_file.stat().st_size > 100

    small_file = tmp_path / "small.sql"
    small_file.write_text("CREATE TABLE small (id INT);")

    discovered = scanner.scan(tmp_path)
    assert small_file in discovered.sql_files
    assert huge_file in discovered.oversized_files
