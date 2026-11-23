"""Configuration path resolution for Support."""

import os
from pathlib import Path
import platformdirs

DEFAULT_DATABASE_FILENAME = "profiles.sqlite3"
SUPPORT_HOME_ENV_VAR = "SUPPORT_HOME"


def get_database_path(override_path: Path | str | None = None) -> Path:
    """Resolve the SQLite database path for profiles.

    Resolution order:
    1. Explicit override_path (if provided).
    2. SUPPORT_HOME environment variable (as the root directory for profiles.sqlite3).
    3. User configuration directory via platformdirs.
    """
    if override_path is not None:
        path = Path(override_path)
        if path.is_dir():
            return path / DEFAULT_DATABASE_FILENAME
        return path

    support_home = os.environ.get(SUPPORT_HOME_ENV_VAR)
    if support_home:
        return Path(support_home) / DEFAULT_DATABASE_FILENAME

    return platformdirs.user_config_path("support", appauthor=False) / DEFAULT_DATABASE_FILENAME
