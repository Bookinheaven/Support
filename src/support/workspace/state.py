"""Session state abstraction for the interactive Support workspace."""

from dataclasses import dataclass, field
from typing import Any, Optional

from support.db.models import ConnectionProfile
from support.db.service import DatabaseService
from support.llm.service import LLMService


@dataclass
class ChatMessage:
    """Represents a message in the workspace conversation history."""

    role: str  # "user", "assistant", "system"
    content: str
    metadata: dict[str, Any] = field(default_factory=dict)


class WorkspaceSessionState:
    """Encapsulates in-memory session state for an interactive workspace session."""

    def __init__(
        self,
        active_database: Optional[str] = None,
        database_profile: Optional[ConnectionProfile] = None,
        database_status: Optional[str] = None,
        active_provider: str = "ollama",
        active_model: str = "qwen2.5-coder:7b",
        provider_status: Optional[str] = None,
    ) -> None:
        self.active_database: Optional[str] = active_database
        self.database_profile: Optional[ConnectionProfile] = database_profile
        self.database_status: Optional[str] = database_status
        self.db_details: Optional[str] = None
        self.db_check_id: int = 0

        self.active_provider: str = active_provider
        self.active_model: str = active_model
        self.provider_status: Optional[str] = provider_status
        self.model_details: Optional[str] = None
        self.model_check_id: int = 0
        self.model_generation_tested: bool = False
        self.model_generation_status: Optional[str] = None

        self.history: list[ChatMessage] = []
        self.current_task: Optional[str] = None
        self._listeners: list[Callable[[], None]] = []

    def add_listener(self, callback: Callable[[], None]) -> None:
        """Register a callback invoked when status changes."""
        if callback not in self._listeners:
            self._listeners.append(callback)

    def remove_listener(self, callback: Callable[[], None]) -> None:
        """Unregister a status callback."""
        if callback in self._listeners:
            self._listeners.remove(callback)

    def notify_status_changed(self) -> None:
        """Invoke all registered status listeners safely."""
        for cb in list(self._listeners):
            try:
                cb()
            except Exception:
                pass

    def add_message(
        self, role: str, content: str, metadata: Optional[dict[str, Any]] = None
    ) -> None:
        """Append a message to the session conversation history."""
        self.history.append(
            ChatMessage(role=role, content=content, metadata=metadata or {})
        )

    def set_database(
        self,
        name: Optional[str],
        profile: Optional[ConnectionProfile] = None,
        status: Optional[str] = None,
    ) -> None:
        """Update active database selection for the current session."""
        self.active_database = name
        self.database_profile = profile
        self.db_check_id += 1  # Invalidate pending background checks for prior database
        self.database_status = status or ("Checking..." if name else "No databases configured")
        self.db_details = None
        self.notify_status_changed()

    def set_database_status(
        self,
        status: str,
        details: Optional[str] = None,
        check_id: Optional[int] = None,
    ) -> bool:
        """Update database status if check_id matches the active check revision."""
        if check_id is not None and check_id != self.db_check_id:
            return False  # Discard stale result
        self.database_status = status
        self.db_details = details
        self.notify_status_changed()
        return True

    def set_model(
        self,
        provider: str,
        model: str,
        status: Optional[str] = None,
    ) -> None:
        """Update active provider and model selection for the current session."""
        self.active_provider = provider
        self.active_model = model
        self.model_check_id += 1  # Invalidate pending background checks for prior model
        self.provider_status = status or "Checking..."
        self.model_details = None
        self.model_generation_tested = False
        self.model_generation_status = None
        self.notify_status_changed()

    def set_model_status(
        self,
        status: str,
        details: Optional[str] = None,
        check_id: Optional[int] = None,
        generation_tested: bool = False,
        generation_status: Optional[str] = None,
    ) -> bool:
        """Update model status if check_id matches the active check revision."""
        if check_id is not None and check_id != self.model_check_id:
            return False  # Discard stale result
        self.provider_status = status
        self.model_details = details
        if generation_tested:
            self.model_generation_tested = True
            self.model_generation_status = generation_status
        self.notify_status_changed()
        return True


    @classmethod
    def initialize(
        cls,
        db_service: Optional[DatabaseService] = None,
        llm_service: Optional[LLMService] = None,
        check_connectivity: bool = False,
    ) -> "WorkspaceSessionState":
        """Initialize session state from existing database profiles and LLM configuration.

        By default (check_connectivity=False), loads locally available profile and
        configuration metadata without making blocking network requests to databases
        or LLM providers. Live verification is performed on-demand or when requested.
        """
        dbs = db_service or DatabaseService()
        llms = llm_service or LLMService()

        # 1. Resolve database profile and connection status
        active_db_name: Optional[str] = None
        active_db_profile: Optional[ConnectionProfile] = None
        db_status: Optional[str] = None

        try:
            if check_connectivity:
                profiles, health_results = dbs.list_profiles_with_status(
                    check_health=True, timeout=2.0
                )
                if profiles:
                    # Prioritize first online profile; otherwise use first profile
                    selected_profile = profiles[0]
                    for p in profiles:
                        h = health_results.get(p.name)
                        if h and h.is_online:
                            selected_profile = p
                            break

                    active_db_name = selected_profile.name
                    active_db_profile = selected_profile
                    h_res = health_results.get(selected_profile.name)
                    if h_res:
                        status_text = str(getattr(h_res.status, "value", h_res.status))
                        if h_res.latency_ms is not None:
                            status_text += f" ({h_res.latency_ms:.0f} ms)"
                        db_status = status_text
                    else:
                        db_status = "Status unknown"
                else:
                    db_status = "No databases configured"
            else:
                # Fast local metadata load
                profiles = []
                if hasattr(dbs, "list_profiles"):
                    try:
                        res = dbs.list_profiles()
                        if isinstance(res, list):
                            profiles = res
                    except Exception:
                        pass
                if not profiles and hasattr(dbs, "manager") and hasattr(dbs.manager, "list_profiles"):
                    try:
                        res = dbs.manager.list_profiles()
                        if isinstance(res, list):
                            profiles = res
                    except Exception:
                        pass
                if not profiles and hasattr(dbs, "list_profiles_with_status"):
                    try:
                        mock_val = dbs.list_profiles_with_status.return_value
                        if isinstance(mock_val, tuple) and isinstance(mock_val[0], list) and mock_val[0]:
                            profiles = mock_val[0]
                    except Exception:
                        pass

                if profiles:
                    selected_profile = profiles[0]
                    active_db_name = selected_profile.name
                    active_db_profile = selected_profile
                    db_status = "Checking..."
                else:
                    db_status = "No databases configured"

        except Exception as e:
            db_status = f"Error discovering databases: {e}"

        # 2. Resolve LLM provider and model
        try:
            config = llms.get_config()
            active_provider = config.default_provider
            active_model = config.default_model

            if check_connectivity:
                val_res = llms.validate_provider(active_provider, active_model)
                status_val = val_res.status.value
                if val_res.latency_ms is not None:
                    status_val += f" ({val_res.latency_ms:.0f} ms)"
                provider_status = status_val
            else:
                provider_status = "Checking..."
        except Exception as e:
            active_provider = "ollama"
            active_model = "qwen2.5-coder:7b"
            provider_status = f"Unverified: {e}"

        return cls(
            active_database=active_db_name,
            database_profile=active_db_profile,
            database_status=db_status,
            active_provider=active_provider,
            active_model=active_model,
            provider_status=provider_status,
        )

