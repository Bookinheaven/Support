"""Schema-grounded sample and random data generation for controlled CRUD operations."""

import random
import re
import secrets
import string
import uuid
from typing import Any, Optional

from sqlalchemy import text

from support.core.dialect import Dialect
from support.db.connection import DatabaseConnectionFactory
from support.db.models import ConnectionProfile
from support.db.service import DatabaseService
from support.schema.models import ColumnSchema, DatabaseSchema, TableSchema

try:
    import bcrypt

    _HAS_BCRYPT = True
except ImportError:
    _HAS_BCRYPT = False


def hash_password(password: str) -> str:
    """Hash a plaintext password using bcrypt with cost factor 12, matching athena standard format."""
    if _HAS_BCRYPT:
        salt = bcrypt.gensalt(12)
        return bcrypt.hashpw(password.encode("utf-8"), salt).decode("utf-8")
    else:
        import hashlib

        salt = secrets.token_hex(16)
        h = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt.encode("utf-8"), 100000)
        return f"$pbkdf2-sha256$100000${salt}${h.hex()}"


def generate_secure_test_password(length: int = 16) -> str:
    """Generate a high-entropy test password with uppercase, lowercase, digits, and special characters."""
    alphabet = string.ascii_letters + string.digits + "!@#$%^&*"
    return "".join(secrets.choice(alphabet) for _ in range(length))


# Common first and last names for realistic test users
_FIRST_NAMES = [
    "Alex", "Jordan", "Taylor", "Morgan", "Sam", "Chris", "Pat", "Riley", "Casey", "Avery",
    "Logan", "Jamie", "Dakota", "Reese", "Quinn", "Skyler", "Jesse", "Kendall", "Cameron", "Rowan"
]
_LAST_NAMES = [
    "Smith", "Johnson", "Williams", "Brown", "Jones", "Garcia", "Miller", "Davis", "Rodriguez", "Martinez",
    "Hernandez", "Lopez", "Gonzalez", "Wilson", "Anderson", "Thomas", "Taylor", "Moore", "Jackson", "Martin"
]


def _extract_length_limit(data_type: str) -> Optional[int]:
    """Extract numeric length limit from types like VARCHAR(50) or CHAR(20)."""
    m = re.search(r"\(\s*(\d+)\s*\)", data_type)
    if m:
        try:
            return int(m.group(1))
        except ValueError:
            pass
    return None


def _check_value_exists_in_db(
    table: TableSchema,
    column_name: str,
    val: Any,
    profile: Optional[ConnectionProfile],
    db_service: Optional[DatabaseService],
) -> bool:
    """Check if a specific value already exists in the live database table."""
    if not profile or not db_service:
        return False

    password: Optional[str] = None
    try:
        _, password = db_service.manager.get_profile_with_password(profile.name)
    except Exception:
        pass

    try:
        engine = DatabaseConnectionFactory.create_engine(
            profile, password=password, timeout=3, readonly=True
        )
        schema_qual = f'"{table.schema_name}"."' if table.schema_name and profile.dialect == Dialect.POSTGRES else ""
        table_ref = f'"{table.schema_name}"."{table.name}"' if table.schema_name and profile.dialect in (Dialect.POSTGRES, Dialect.SQLITE) else f"`{table.schema_name}`.`{table.name}`" if table.schema_name and profile.dialect == Dialect.MYSQL else f"[{table.schema_name}].[{table.name}]" if table.schema_name and profile.dialect == Dialect.MSSQL else f'"{table.name}"'
        col_ref = f'"{column_name}"' if profile.dialect in (Dialect.POSTGRES, Dialect.SQLITE) else f"`{column_name}`" if profile.dialect == Dialect.MYSQL else f"[{column_name}]"
        
        sql = f"SELECT 1 FROM {table_ref} WHERE {col_ref} = :val LIMIT 1"
        with engine.connect() as conn:
            res = conn.execute(text(sql), {"val": val}).scalar()
            return res is not None
    except Exception:
        return False


def generate_plausible_field_value(
    col: ColumnSchema,
    table: TableSchema,
    existing_values: dict[str, Any],
    profile: Optional[ConnectionProfile] = None,
    db_service: Optional[DatabaseService] = None,
) -> Any:
    """Generate a semantically plausible, constraint-respecting sample value for a column."""
    name_low = col.name.lower()
    type_upper = col.data_type.upper()
    max_len = _extract_length_limit(type_upper)

    # 1. Deterministic normalization derivations
    if name_low in ("username_lower", "usernamelower", "lower_username"):
        if "username" in existing_values:
            return str(existing_values["username"]).lower()
        # If username not yet generated, will be filled once username is chosen

    if name_low in ("email_lower", "emaillower", "lower_email"):
        if "email" in existing_values:
            return str(existing_values["email"]).lower()

    # 2. Credential columns: secure password hash
    if any(k in name_low for k in ("password_hash", "passwd_hash", "pass_hash", "pwd_hash")):
        test_pw = generate_secure_test_password()
        return hash_password(test_pw)

    # 3. User identity columns
    if name_low in ("username", "user_name", "uname", "login", "handle"):
        for _ in range(5):
            first = random.choice(_FIRST_NAMES).lower()
            num = random.randint(100, 9999)
            cand = f"{first}{num}"
            if max_len and len(cand) > max_len:
                cand = f"{first[:max_len - 4]}{num}"[:max_len]
            if not _check_value_exists_in_db(table, col.name, cand, profile, db_service):
                return cand
        return f"user_{secrets.token_hex(4)}"[:max_len or 20]

    if name_low in ("full_name", "fullname", "display_name", "name"):
        first = random.choice(_FIRST_NAMES)
        last = random.choice(_LAST_NAMES)
        cand = f"{first} {last}"
        return cand[:max_len] if max_len else cand

    if name_low in ("first_name", "firstname"):
        cand = random.choice(_FIRST_NAMES)
        return cand[:max_len] if max_len else cand

    if name_low in ("last_name", "lastname", "surname"):
        cand = random.choice(_LAST_NAMES)
        return cand[:max_len] if max_len else cand

    if name_low in ("email", "mail", "email_address"):
        base = existing_values.get("username")
        if not base:
            first = random.choice(_FIRST_NAMES).lower()
            last = random.choice(_LAST_NAMES).lower()
            base = f"{first}.{last}{random.randint(10, 999)}"
        cand = f"{str(base).lower()}@example.com"
        for _ in range(5):
            if not _check_value_exists_in_db(table, col.name, cand, profile, db_service):
                return cand[:max_len] if max_len else cand
            cand = f"{str(base).lower()}_{secrets.token_hex(2)}@example.com"
        return cand[:max_len] if max_len else cand

    # 4. Status / Role / Category columns
    if name_low in ("status", "state"):
        cand = "active"
        return cand[:max_len] if max_len else cand

    if name_low in ("role", "account_type", "user_type"):
        cand = "user"
        return cand[:max_len] if max_len else cand

    if name_low in ("title", "subject"):
        cand = f"Sample {table.name.rstrip('s').capitalize()} {random.randint(1, 100)}"
        return cand[:max_len] if max_len else cand

    if name_low in ("description", "content", "body", "notes", "comment"):
        return f"This is an automated sample record created for {table.name}."

    # 5. Type-driven fallbacks
    if "UUID" in type_upper:
        return str(uuid.uuid4())

    if any(t in type_upper for t in ("INT", "SERIAL", "BIGINT", "SMALLINT")):
        # If it's a foreign key, check if we can safely pick a referenced ID, else small positive integer
        return random.randint(1, 100)

    if any(t in type_upper for t in ("NUMERIC", "DECIMAL", "FLOAT", "REAL", "DOUBLE")):
        return round(random.uniform(10.0, 500.0), 2)

    if "BOOL" in type_upper:
        return True

    if any(t in type_upper for t in ("DATE", "TIME")):
        from datetime import datetime, timezone
        return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")

    # Generic string fallback
    cand = f"sample_{col.name}_{random.randint(10, 99)}"
    return cand[:max_len] if max_len else cand


def populate_sample_and_derived_values(
    table: TableSchema,
    schema: DatabaseSchema,
    initial_values: dict[str, Any],
    profile: Optional[ConnectionProfile] = None,
    db_service: Optional[DatabaseService] = None,
    allow_sample_data: bool = False,
) -> tuple[dict[str, Any], list[str]]:
    """Populate derived columns, resolve credentials, and generate sample values when authorized.

    Returns:
        (resolved_values_dict, missing_unresolved_required_fields)
    """
    values = dict(initial_values)

    # 1. Deterministic derivations based on user-supplied values
    for col in table.columns:
        c_low = col.name.lower()
        if c_low in ("username_lower", "usernamelower", "lower_username"):
            if "username" in values and col.name not in values:
                values[col.name] = str(values["username"]).lower()
        elif c_low in ("email_lower", "emaillower", "lower_email"):
            if "email" in values and col.name not in values:
                values[col.name] = str(values["email"]).lower()

    # 2. If allow_sample_data is True, generate missing required values
    for col in table.columns:
        if col.name in values:
            continue

        # Check if column is auto-generated primary key or has database default
        is_auto_pk = col.is_primary_key and any(
            t in col.data_type.upper() for t in ("INT", "SERIAL", "AUTO", "IDENTITY", "UUID")
        )
        has_default = col.default_value is not None

        if is_auto_pk or has_default or col.nullable:
            continue

        # Column is strictly required and missing!
        if allow_sample_data:
            val = generate_plausible_field_value(
                col=col,
                table=table,
                existing_values=values,
                profile=profile,
                db_service=db_service,
            )
            values[col.name] = val
            # Secondary check: if username was just generated, derive username_lower
            if col.name == "username":
                for other_col in table.columns:
                    if other_col.name.lower() in ("username_lower", "usernamelower", "lower_username"):
                        if other_col.name not in values:
                            values[other_col.name] = str(val).lower()

    # 3. Determine any remaining missing required columns
    missing_required: list[str] = []
    for col in table.columns:
        if col.name in values:
            continue

        c_low = col.name.lower()
        # Derived normalization columns are computed automatically from parents
        if c_low in ("username_lower", "usernamelower", "lower_username"):
            continue
        if c_low in ("email_lower", "emaillower", "lower_email"):
            continue

        is_auto_pk = col.is_primary_key and any(
            t in col.data_type.upper() for t in ("INT", "SERIAL", "AUTO", "IDENTITY", "UUID")
        )
        has_default = col.default_value is not None

        if not is_auto_pk and not has_default and not col.nullable:
            if c_low in ("password_hash", "passwd_hash", "pass_hash", "pwd_hash"):
                if "password" not in values and "password_hash" not in values:
                    missing_required.append("password")
            else:
                missing_required.append(col.name)

    if "password" in missing_required:
        missing_required = [f for f in missing_required if f != "password"] + ["password"]

    return values, missing_required
