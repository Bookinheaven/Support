"""Background connectivity and health check coordinator for Support interactive workspace."""

import threading
from typing import Optional
from support.db.health import check_connection_health
from support.db.service import DatabaseService
from support.llm.models import ProviderStatus
from support.llm.service import LLMService
from support.workspace.state import WorkspaceSessionState


class WorkspaceStatusChecker:
    """Coordinates asynchronous health verification of active database and LLM model.

    Ensures:
    1. Independent execution: slow database checks do not delay LLM model validation.
    2. Stale result rejection: revision IDs prevent obsolete checks from overwriting current state.
    3. Safe credential handling: passwords and connection secrets are redacted.
    4. Clean shutdown: worker threads are daemons and respond to shutdown events.
    """

    DB_TIMEOUT_SECONDS: float = 3.0
    LLM_TIMEOUT_SECONDS: float = 4.0

    def __init__(
        self,
        state: WorkspaceSessionState,
        db_service: Optional[DatabaseService] = None,
        llm_service: Optional[LLMService] = None,
    ) -> None:
        self.state = state
        self.db_service = db_service or DatabaseService()
        self.llm_service = llm_service or LLMService()

        self._shutdown_event = threading.Event()
        self._db_lock = threading.Lock()
        self._model_lock = threading.Lock()

        self._db_thread: Optional[threading.Thread] = None
        self._model_thread: Optional[threading.Thread] = None

    def start_all(self) -> None:
        """Start both database and model checks concurrently in background daemon threads."""
        self.check_database()
        self.check_model()

    def refresh(self) -> None:
        """Explicitly refresh checks for both active database and configured model."""
        # Advance check IDs to cancel any pending results
        self.state.db_check_id += 1
        self.state.model_check_id += 1
        self.start_all()

    def check_database(self) -> None:
        """Initiate background connectivity verification for the active database profile."""
        with self._db_lock:
            target_profile = self.state.database_profile
            target_name = self.state.active_database

            if not target_name or not target_profile:
                self.state.set_database_status("None selected", details="Use /db to select an active database.")
                return

            check_id = self.state.db_check_id
            self.state.set_database_status("Checking...", check_id=check_id)

            def _worker() -> None:
                if self._shutdown_event.is_set():
                    return
                try:
                    password = None
                    try:
                        _, password = self.db_service.manager.get_profile_with_password(target_profile.name)
                    except Exception:
                        pass

                    health = check_connection_health(
                        target_profile,
                        password=password,
                        timeout=self.DB_TIMEOUT_SECONDS,
                    )

                    if self._shutdown_event.is_set():
                        return

                    if health.is_online:
                        lat_str = f" · {health.latency_ms:.0f}ms" if health.latency_ms is not None else ""
                        status = f"Online{lat_str}"
                        details = None
                    elif health.status == "AUTH FAILED":
                        status = "Authentication failed"
                        details = health.hint or health.error_message
                    elif health.status == "TIMEOUT":
                        status = "Timeout"
                        details = health.hint or f"Connection timed out after {self.DB_TIMEOUT_SECONDS:.0f}s"
                    elif health.status == "OFFLINE":
                        status = "Offline"
                        details = health.hint or health.error_message
                    else:
                        status = "Unreachable"
                        details = health.hint or health.error_message

                    self.state.set_database_status(status, details=details, check_id=check_id)
                except Exception as e:
                    if not self._shutdown_event.is_set():
                        self.state.set_database_status(f"Error: {e}", details=str(e), check_id=check_id)

            t = threading.Thread(target=_worker, name="Support-DB-Checker", daemon=True)
            self._db_thread = t
            t.start()

    def check_model(self) -> None:
        """Initiate background validation for the active LLM provider and model."""
        with self._model_lock:
            target_provider = self.state.active_provider
            target_model = self.state.active_model

            check_id = self.state.model_check_id
            self.state.set_model_status("Checking...", check_id=check_id)

            def _worker() -> None:
                if self._shutdown_event.is_set():
                    return
                try:
                    val = self.llm_service.validate_provider(
                        provider=target_provider,
                        model=target_model,
                    )

                    if self._shutdown_event.is_set():
                        return

                    if val.status == ProviderStatus.AVAILABLE:
                        status = "Available"
                        details = None
                    elif val.status == ProviderStatus.MODEL_UNAVAILABLE:
                        status = f"Model '{target_model}' not installed"
                        details = val.hint or val.message
                    elif val.status == ProviderStatus.AUTH_FAILED:
                        status = "Authentication failed"
                        details = val.hint or val.message
                    elif val.status == ProviderStatus.NOT_CONFIGURED:
                        status = "Not configured"
                        details = val.hint or val.message
                    elif val.status == ProviderStatus.UNREACHABLE:
                        status = "Provider unreachable"
                        details = val.hint or val.message
                    else:
                        status = f"Unavailable: {val.status.value}"
                        details = val.hint or val.message

                    self.state.set_model_status(
                        status,
                        details=details,
                        check_id=check_id,
                    )
                except Exception as e:
                    if not self._shutdown_event.is_set():
                        self.state.set_model_status(
                            f"Unverified: {e}",
                            details=str(e),
                            check_id=check_id,
                        )

            t = threading.Thread(target=_worker, name="Support-Model-Checker", daemon=True)
            self._model_thread = t
            t.start()

    def wait_completion(self, timeout: float = 5.0) -> None:
        """Wait for in-flight database and model check threads to complete."""
        threads = []
        with self._db_lock:
            if self._db_thread and self._db_thread.is_alive():
                threads.append(self._db_thread)
        with self._model_lock:
            if self._model_thread and self._model_thread.is_alive():
                threads.append(self._model_thread)

        for t in threads:
            t.join(timeout=timeout)

    def shutdown(self) -> None:
        """Signal background worker threads to terminate cleanly."""
        self._shutdown_event.set()
        self.wait_completion(timeout=1.0)
