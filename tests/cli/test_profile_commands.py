"""CLI tests for support profile command group."""

from pathlib import Path
from typer.testing import CliRunner

from support.cli.main import app

runner = CliRunner()


def test_profile_cli_end_to_end_workflow(tmp_path: Path) -> None:
    env = {"SUPPORT_HOME": str(tmp_path)}

    # 1. Empty list
    res = runner.invoke(app, ["profile", "list"], env=env)
    assert res.exit_code == 0
    assert "No profiles found" in res.output

    # 2. Add profile
    add_args = [
        "profile",
        "add",
        "--name",
        "local-dev",
        "--database",
        "myapp_db",
        "--username",
        "app_user",
        "--host",
        "127.0.0.1",
        "--port",
        "5433",
        "--ssl-mode",
        "prefer",
    ]
    res = runner.invoke(app, add_args, env=env)
    assert res.exit_code == 0
    assert "Profile 'local-dev' added successfully." in res.output

    # 3. List shows the profile
    res = runner.invoke(app, ["profile", "list"], env=env)
    assert res.exit_code == 0
    assert "local-dev" in res.output
    assert "postgresql" in res.output
    assert "127.0.0.1" in res.output
    assert "myapp_db" in res.output
    assert "app_user" in res.output

    # 4. Show profile
    res = runner.invoke(app, ["profile", "show", "local-dev"], env=env)
    assert res.exit_code == 0
    assert "Profile: local-dev" in res.output
    assert "Host:        127.0.0.1" in res.output
    assert "Port:        5433" in res.output
    assert "Database:    myapp_db" in res.output
    assert "Username:    app_user" in res.output
    assert "SSL Mode:    prefer" in res.output

    # Extract ID
    for line in res.output.splitlines():
        if "ID:" in line:
            original_id = line.split("ID:")[1].strip()
            break
    assert original_id

    # 5. Rename profile
    res = runner.invoke(app, ["profile", "rename", "local-dev", "local-prod"], env=env)
    assert res.exit_code == 0
    assert "Profile 'local-dev' renamed to 'local-prod'." in res.output

    # Old name not found
    res = runner.invoke(app, ["profile", "show", "local-dev"], env=env)
    assert res.exit_code == 1
    assert "Error: Profile 'local-dev' not found." in res.output

    # New name has preserved ID
    res = runner.invoke(app, ["profile", "show", "local-prod"], env=env)
    assert res.exit_code == 0
    assert original_id in res.output

    # 6. Remove profile with confirmation declined (input 'n')
    res = runner.invoke(app, ["profile", "remove", "local-prod"], input="n\n", env=env)
    assert res.exit_code == 0
    assert "Aborted." in res.output

    # Verify profile still exists
    res = runner.invoke(app, ["profile", "show", "local-prod"], env=env)
    assert res.exit_code == 0

    # 7. Remove profile with --yes flag
    res = runner.invoke(app, ["profile", "remove", "local-prod", "--yes"], env=env)
    assert res.exit_code == 0
    assert "Profile 'local-prod' removed." in res.output

    # 8. List is empty again
    res = runner.invoke(app, ["profile", "list"], env=env)
    assert res.exit_code == 0
    assert "No profiles found" in res.output


def test_profile_cli_errors_handled_cleanly(tmp_path: Path) -> None:
    env = {"SUPPORT_HOME": str(tmp_path)}

    # Add a valid profile
    res = runner.invoke(
        app,
        ["profile", "add", "--name", "dev-db", "--database", "db", "--username", "u", "--ssl-mode", "prefer"],
        env=env,
    )
    assert res.exit_code == 0

    # Duplicate profile add fails without traceback
    res = runner.invoke(
        app,
        ["profile", "add", "--name", "dev-db", "--database", "db", "--username", "u", "--ssl-mode", "prefer"],
        env=env,
    )
    assert res.exit_code == 1
    assert "Error: A profile with name 'dev-db' already exists." in res.output
    assert "Traceback" not in res.output

    # Invalid profile name (uppercase) fails domain validation without traceback
    res = runner.invoke(
        app,
        ["profile", "add", "--name", "INVALID_NAME", "--database", "db", "--username", "u", "--ssl-mode", "prefer"],
        env=env,
    )
    assert res.exit_code == 1
    assert "Error: Invalid profile name 'INVALID_NAME'" in res.output
    assert "Traceback" not in res.output

    # Show non-existent profile fails without traceback
    res = runner.invoke(app, ["profile", "show", "non-existent"], env=env)
    assert res.exit_code == 1
    assert "Error: Profile 'non-existent' not found." in res.output
    assert "Traceback" not in res.output

    # Remove non-existent profile fails without traceback
    res = runner.invoke(app, ["profile", "remove", "non-existent", "--yes"], env=env)
    assert res.exit_code == 1
    assert "Error: Profile 'non-existent' not found." in res.output
    assert "Traceback" not in res.output


def test_profile_cli_multi_database_types(tmp_path: Path) -> None:
    env = {"SUPPORT_HOME": str(tmp_path)}

    # Add SQLite profile
    res = runner.invoke(
        app,
        ["profile", "add", "--name", "local-sqlite", "--type", "sqlite", "--database-path", "/var/data/app.db"],
        env=env,
    )
    assert res.exit_code == 0
    assert "Profile 'local-sqlite' added successfully." in res.output

    # Add MySQL profile
    res = runner.invoke(
        app,
        ["profile", "add", "--name", "my-mysql", "--type", "mysql", "--database", "shop", "--username", "admin"],
        env=env,
    )
    assert res.exit_code == 0
    assert "Profile 'my-mysql' added successfully." in res.output

    # List profiles
    res = runner.invoke(app, ["profile", "list"], env=env)
    assert res.exit_code == 0
    assert "local-sqlite" in res.output
    assert "sqlite" in res.output
    assert "my-mysql" in res.output
    assert "mysql" in res.output

    # Show SQLite profile
    res = runner.invoke(app, ["profile", "show", "local-sqlite"], env=env)
    assert res.exit_code == 0
    assert "Type:        sqlite" in res.output
    assert "Path:        /var/data/app.db" in res.output

    # Setting password on SQLite profile fails
    res = runner.invoke(
        app,
        ["profile", "password", "set", "local-sqlite"],
        input="secret\nsecret\n",
        env=env,
    )
    assert res.exit_code == 1
    assert "does not support passwords" in res.output
