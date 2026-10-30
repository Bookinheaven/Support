"""Bounded, local, persistent query history storage with privacy protections."""

import json
import os
from pathlib import Path
from typing import Optional

from support.history.models import QueryHistoryEntry


class QueryHistoryStore:
    """Manages reading, persisting, and bounding query history in ~/.support/query_history.json.

    Safeguards:
    - Never stores credentials or query result rows.
    - Applies restricted file permissions (0o600).
    - Configurable bounded retention limit (default 100 entries).
    - Can be completely disabled via SUPPORT_HISTORY_ENABLED=false or enabled=False.
    - Handles malformed or corrupt history files gracefully without crashing.
    """

    DEFAULT_MAX_ENTRIES = 100

    def __init__(
        self,
        config_dir: Optional[Path] = None,
        max_entries: Optional[int] = None,
        enabled: Optional[bool] = None,
    ) -> None:
        if config_dir is not None:
            self.config_dir = Path(config_dir)
        else:
            env_dir = os.getenv("SUPPORT_CONFIG_DIR")
            self.config_dir = Path(env_dir) if env_dir else Path.home() / ".support"

        self.history_file = self.config_dir / "query_history.json"

        # Determine if history is enabled
        if enabled is not None:
            self.enabled = enabled
        else:
            env_enabled = os.getenv("SUPPORT_HISTORY_ENABLED", "true").strip().lower()
            self.enabled = env_enabled not in ("0", "false", "no", "off", "disable", "disabled")

        # Determine retention limit
        if max_entries is not None:
            self.max_entries = max(1, max_entries)
        else:
            env_max = os.getenv("SUPPORT_HISTORY_MAX_ENTRIES")
            if env_max and env_max.isdigit():
                self.max_entries = max(1, int(env_max))
            else:
                self.max_entries = self.DEFAULT_MAX_ENTRIES

        self._ensure_storage_exists()

    def _ensure_storage_exists(self) -> None:
        """Ensure storage directory exists with proper permissions."""
        if not self.enabled:
            return
        try:
            self.config_dir.mkdir(parents=True, exist_ok=True)
            if not self.history_file.exists():
                self._write_raw([])
            else:
                self._restrict_permissions()
        except Exception:
            pass

    def _restrict_permissions(self) -> None:
        """Restrict file permissions to current user (0o600) where supported."""
        try:
            if self.history_file.exists():
                os.chmod(self.history_file, 0o600)
        except Exception:
            pass

    def _read_raw(self) -> list[dict]:
        """Safely read raw JSON entries from disk, returning an empty list on corruption."""
        if not self.enabled or not self.history_file.exists():
            return []
        try:
            content = self.history_file.read_text(encoding="utf-8")
            if not content.strip():
                return []
            data = json.loads(content)
            if isinstance(data, list):
                return data
            return []
        except Exception:
            # Handle malformed / corrupted history files gracefully
            return []

    def _write_raw(self, entries: list[dict]) -> None:
        """Safely write raw JSON entries to disk and restrict permissions."""
        if not self.enabled:
            return
        try:
            self.config_dir.mkdir(parents=True, exist_ok=True)
            self.history_file.write_text(json.dumps(entries, indent=2, default=str), encoding="utf-8")
            self._restrict_permissions()
        except Exception:
            pass

    def list_entries(self) -> list[QueryHistoryEntry]:
        """Retrieve all stored history entries in chronological order."""
        if not self.enabled:
            return []
        raw_list = self._read_raw()
        valid_entries: list[QueryHistoryEntry] = []
        for item in raw_list:
            try:
                valid_entries.append(QueryHistoryEntry.model_validate(item))
            except Exception:
                continue
        return valid_entries

    def get_entry(self, index: int) -> Optional[QueryHistoryEntry]:
        """Get entry by 1-based index."""
        entries = self.list_entries()
        if 1 <= index <= len(entries):
            return entries[index - 1]
        return None

    def add_entry(
        self,
        question: str,
        sql: str,
        database: str,
        dialect: str,
        explanation: Optional[str] = None,
        is_validated: bool = False,
        validation_status: str = "Not validated",
        is_schema_verified: Optional[bool] = None,
        schema_status: Optional[str] = None,
        execution_status: Optional[str] = None,
        duration_ms: Optional[float] = None,
        row_count: Optional[int] = None,
        error: Optional[str] = None,
        entry_type: str = "query",
    ) -> Optional[QueryHistoryEntry]:
        """Record a query draft and its execution outcome to bounded history.

        Trims oldest entries if size exceeds max_entries.
        """
        if not self.enabled:
            return None

        entries = self.list_entries()
        next_id = (entries[-1].id + 1) if entries else 1

        new_entry = QueryHistoryEntry(
            id=next_id,
            question=question,
            sql=sql,
            explanation=explanation,
            database=database,
            dialect=dialect,
            is_validated=is_validated,
            validation_status=validation_status,
            is_schema_verified=is_schema_verified,
            schema_status=schema_status,
            execution_status=execution_status,
            duration_ms=duration_ms,
            row_count=row_count,
            error=error,
            entry_type=entry_type,
        )

        entries.append(new_entry)
        if len(entries) > self.max_entries:
            entries = entries[-self.max_entries:]

        raw_data = [e.model_dump(mode="json") for e in entries]
        self._write_raw(raw_data)
        return new_entry

    def update_last_execution(
        self,
        sql: str,
        execution_status: str,
        duration_ms: Optional[float] = None,
        row_count: Optional[int] = None,
        error: Optional[str] = None,
    ) -> bool:
        """Update the execution outcome of the most matching recent query draft."""
        if not self.enabled:
            return False

        entries = self.list_entries()
        if not entries:
            return False

        # Find the most recent entry with matching SQL
        for entry in reversed(entries):
            if entry.sql.strip() == sql.strip():
                entry.execution_status = execution_status
                entry.duration_ms = duration_ms
                entry.row_count = row_count
                entry.error = error
                raw_data = [e.model_dump(mode="json") for e in entries]
                self._write_raw(raw_data)
                return True

        return False

    def clear(self) -> None:
        """Clear all stored query history."""
        self._write_raw([])

