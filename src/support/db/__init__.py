"""Database profiles and connection management package."""

from support.db.models import ConnectionProfile
from support.db.keyring_store import KeyringStore
from support.db.manager import ProfileManager, ProfileAlreadyExistsError, ProfileNotFoundError
from support.db.connection import DatabaseConnectionFactory
from support.db.url_parser import parse_database_url, infer_profile_name
from support.db.health import HealthCheckResult, check_connection_health, check_profiles_concurrently
from support.db.service import DatabaseService, ConnectionVerificationError

__all__ = [
    "ConnectionProfile",
    "KeyringStore",
    "ProfileManager",
    "ProfileAlreadyExistsError",
    "ProfileNotFoundError",
    "DatabaseConnectionFactory",
    "parse_database_url",
    "infer_profile_name",
    "HealthCheckResult",
    "check_connection_health",
    "check_profiles_concurrently",
    "DatabaseService",
    "ConnectionVerificationError",
]
