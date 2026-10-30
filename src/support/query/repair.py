"""Bounded, schema-grounded query correction and error recovery."""

import difflib
import re
from typing import Optional

from support.core.dialect import Dialect
from support.schema.models import DatabaseSchema


def find_unambiguous_schema_match(
    unknown_name: str,
    valid_candidates: list[str],
    cutoff: float = 0.75,
) -> tuple[Optional[str], bool]:
    """Find a single unambiguous schema candidate for an unknown identifier.

    Returns:
        (suggested_name, is_unambiguous)
        - If exactly one candidate is plausible: (name, True)
        - If multiple candidates are plausible: (None, False) [Do not guess!]
        - If no candidate is plausible: (None, True)
    """
    if not unknown_name or not valid_candidates:
        return None, True

    norm_target = unknown_name.strip().lower()
    candidate_map = {c.lower(): c for c in valid_candidates}

    # 1. Exact case-insensitive match
    if norm_target in candidate_map:
        return candidate_map[norm_target], True

    # 2. Common plural / singular normalization
    # target 'user' -> candidate 'users'
    plural_target = norm_target + "s"
    if plural_target in candidate_map:
        return candidate_map[plural_target], True

    # target 'users' -> candidate 'user'
    if norm_target.endswith("s") and norm_target[:-1] in candidate_map:
        return candidate_map[norm_target[:-1]], True

    # 3. Deterministic similarity matching with ambiguity detection
    close_matches = difflib.get_close_matches(
        norm_target,
        list(candidate_map.keys()),
        n=3,
        cutoff=cutoff,
    )

    if len(close_matches) == 1:
        return candidate_map[close_matches[0]], True
    elif len(close_matches) > 1:
        # Check if top match is significantly better than second match
        ratio_1 = difflib.SequenceMatcher(None, norm_target, close_matches[0]).ratio()
        ratio_2 = difflib.SequenceMatcher(None, norm_target, close_matches[1]).ratio()
        if ratio_1 >= 0.85 and (ratio_1 - ratio_2) >= 0.20:
            return candidate_map[close_matches[0]], True
        # Ambiguous: multiple plausible candidates, do not guess
        return None, False

    return None, True


def is_recoverable_execution_error(
    error_message: str,
    is_timeout: bool = False,
    is_permission: bool = False,
    is_readonly: bool = False,
) -> bool:
    """Determine whether a database execution failure is an SQL defect that can be repaired."""
    if is_timeout or is_permission or is_readonly:
        return False

    if not error_message:
        return False

    low = error_message.lower()

    # Non-recoverable infrastructure / environment / permission / timeout errors
    non_recoverable_indicators = (
        "connection refused",
        "could not connect",
        "failed to establish a new connection",
        "network is unreachable",
        "host is down",
        "timed out",
        "timeout",
        "canceling statement due to statement timeout",
        "max_execution_time exceeded",
        "access denied",
        "permission denied",
        "login failed",
        "password authentication failed",
        "read-only transaction",
        "attempt to write a readonly database",
        "database file does not exist",
        "unrestricted write privileges",
        "interrupted",
    )
    if any(ind in low for ind in non_recoverable_indicators):
        return False

    # Recoverable SQL syntax, table, column, or aggregation defects
    recoverable_indicators = (
        "syntax error",
        "syntaxerror",
        "does not exist",
        "no such table",
        "no such column",
        "unknown column",
        "unknown table",
        "invalid object name",
        "invalid column name",
        "column reference is ambiguous",
        "ambiguous column name",
        "must appear in the group by clause",
        "not a group by expression",
        "function does not exist",
        "data type mismatch",
        "cannot cast",
        "undefined table",
        "undefined column",
        "undefined_table",
        "undefined_column",
        "violates not-null constraint",
        "null value in column",
        "violates check constraint",
        "violates foreign key constraint",
    )
    return any(ind in low for ind in recoverable_indicators)


def detect_intended_operation(sql: str, question: str = "") -> str:
    """Infer the intended SQL operation from SQL text and/or natural language request."""
    clean_sql = (sql or "").strip().upper()
    if clean_sql.startswith("CREATE TABLE"):
        return "CREATE TABLE"
    elif clean_sql.startswith("CREATE INDEX") or clean_sql.startswith("CREATE UNIQUE INDEX"):
        return "CREATE INDEX"
    elif clean_sql.startswith("ALTER TABLE"):
        return "ALTER TABLE"
    elif clean_sql.startswith("DROP TABLE"):
        return "DROP TABLE"
    elif clean_sql.startswith("INSERT INTO") or clean_sql.startswith("INSERT"):
        return "INSERT"
    elif clean_sql.startswith("UPDATE"):
        return "UPDATE"
    elif clean_sql.startswith("DELETE"):
        return "DELETE"
    elif clean_sql.startswith("SELECT") or clean_sql.startswith("WITH"):
        return "SELECT"

    clean_q = (question or "").lower()
    if any(k in clean_q for k in ("create table", "make a table", "new table", "table for")):
        return "CREATE TABLE"
    if any(k in clean_q for k in ("create index", "add index", "new index")):
        return "CREATE INDEX"
    if any(k in clean_q for k in ("alter table", "add column", "modify column", "drop column")):
        return "ALTER TABLE"
    if any(k in clean_q for k in ("insert into", "add a record", "add a user", "insert a", "add record")):
        return "INSERT"
    if any(k in clean_q for k in ("update", "modify", "change")):
        return "UPDATE"
    if any(k in clean_q for k in ("delete", "remove from", "drop record")):
        return "DELETE"

    return "SELECT"


def build_repair_prompt(
    original_question: str,
    failed_sql: str,
    error_description: str,
    dialect_name: str,
    schema_context: str,
    intended_operation: Optional[str] = None,
    suggestions: Optional[list[str]] = None,
) -> str:
    """Construct a sanitized, bounded prompt requesting SQL draft repair from LLM preserving operation type."""
    sanitized_error = re.sub(r"[\r\n]+", " ", error_description.strip())
    if len(sanitized_error) > 300:
        sanitized_error = sanitized_error[:297] + "..."

    op = (intended_operation or detect_intended_operation(failed_sql, original_question)).upper()

    hint_lines = []
    if suggestions:
        hint_lines.append("SCHEMA CORRECTION HINTS:")
        for s in suggestions:
            hint_lines.append(f"- {s}")

    hints_block = "\n".join(hint_lines) + "\n\n" if hint_lines else ""

    op_rules = {
        "CREATE TABLE": (
            "1. PRESERVE INTENDED OPERATION: The user requested a CREATE TABLE statement. The corrected SQL MUST be a valid CREATE TABLE statement. Do NOT convert it to a SELECT or other statement type.\n"
            "2. Propose clean table and column definitions with valid types and primary keys for the target dialect."
        ),
        "ALTER TABLE": (
            "1. PRESERVE INTENDED OPERATION: The user requested an ALTER TABLE statement. The corrected SQL MUST be a valid ALTER TABLE statement. Do NOT convert it to a SELECT.\n"
            "2. Ensure referenced table and column modifications match the active schema and dialect."
        ),
        "CREATE INDEX": (
            "1. PRESERVE INTENDED OPERATION: The user requested a CREATE INDEX statement. The corrected SQL MUST be a valid CREATE INDEX statement. Do NOT convert it to a SELECT.\n"
            "2. Ensure referenced table and indexed columns exist in the active schema."
        ),
        "INSERT": (
            "1. PRESERVE INTENDED OPERATION: The user requested an INSERT statement. The corrected SQL MUST be a valid INSERT statement. Do NOT convert it to a SELECT query.\n"
            "2. Match column names and data types to the schema."
        ),
        "UPDATE": (
            "1. PRESERVE INTENDED OPERATION: The user requested an UPDATE statement. The corrected SQL MUST be a valid UPDATE statement. Do NOT convert it to a SELECT query.\n"
            "2. Ensure explicit WHERE conditions are present; unconditional updates are prohibited."
        ),
        "DELETE": (
            "1. PRESERVE INTENDED OPERATION: The user requested a DELETE statement. The corrected SQL MUST be a valid DELETE statement. Do NOT convert it to a SELECT query.\n"
            "2. Ensure explicit WHERE conditions are present; unconditional deletes are prohibited."
        ),
        "SELECT": (
            "1. Generate ONLY a single read-only SELECT query.\n"
            "2. Use ONLY table and column names that exist in the DATABASE SCHEMA below.\n"
            "3. NEVER select sensitive credential, password, secret, or token fields."
        ),
    }

    rules_text = op_rules.get(
        op,
        f"1. PRESERVE INTENDED OPERATION: The corrected SQL MUST be a valid {op} statement. Do NOT convert it to a SELECT or other statement type."
    )

    sample_outputs = {
        "CREATE TABLE": "CREATE TABLE <table_name> (\n    id SERIAL PRIMARY KEY,\n    ...\n);",
        "ALTER TABLE": "ALTER TABLE <table_name> ADD COLUMN ...;",
        "CREATE INDEX": "CREATE INDEX <index_name> ON <table_name> (...);",
        "INSERT": "INSERT INTO <table_name> (...) VALUES (...);",
        "UPDATE": "UPDATE <table_name> SET ... WHERE ...;",
        "DELETE": "DELETE FROM <table_name> WHERE ...;",
        "SELECT": "SELECT ...\nFROM ...;",
    }
    sample_block = sample_outputs.get(op, f"{op} ...;")

    return (
        f"You are an expert SQL repair assistant for {dialect_name.upper()}.\n"
        f"An earlier generated SQL statement for the user request below failed validation or execution.\n\n"
        f"CRITICAL RULES:\n"
        f"{rules_text}\n"
        f"2. Adhere strictly to the target database dialect syntax.\n"
        f"3. Do NOT invent unrelated operations. If the request is ambiguous or missing required data, explain what clarification is needed.\n\n"
        f"{hints_block}"
        f"DATABASE SCHEMA:\n"
        f"{schema_context}\n\n"
        f"ORIGINAL REQUEST:\n"
        f"{original_question}\n\n"
        f"FAILED SQL:\n"
        f"```sql\n"
        f"{failed_sql}\n"
        f"```\n\n"
        f"FAILURE REASON:\n"
        f"{sanitized_error}\n\n"
        f"OUTPUT FORMAT:\n"
        f"Respond with a single corrected SQL block followed by explanation:\n"
        f"```sql\n"
        f"{sample_block}\n"
        f"```\n"
        f"Explanation:\n"
        f"<concise description of the fix>\n"
    )


def repair_sql_statement(
    failed_sql: str,
    error_message: str,
    original_question: str,
    dialect_name: str,
    schema_context: str,
    llm_service: Optional[object] = None,
    intended_operation: Optional[str] = None,
    provider: Optional[str] = None,
    model: Optional[str] = None,
    suggestions: Optional[list[str]] = None,
) -> tuple[Optional[str], Optional[str]]:
    """Attempt bounded repair of a failed SQL statement while preserving its operation type.

    Returns:
        (repaired_sql, explanation)
    """
    op = (intended_operation or detect_intended_operation(failed_sql, original_question)).upper()
    prompt = build_repair_prompt(
        original_question=original_question,
        failed_sql=failed_sql,
        error_description=error_message,
        dialect_name=dialect_name,
        schema_context=schema_context,
        intended_operation=op,
        suggestions=suggestions,
    )

    if not llm_service:
        return None, None

    try:
        from support.llm.models import GenerationSettings
        resp = llm_service.generate(
            prompt=prompt,
            provider=provider,
            model=model,
            settings=GenerationSettings(temperature=0.1, timeout=30.0),
        )
        raw_text = resp.text if hasattr(resp, "text") else str(resp)

        # Extract SQL from fenced code block or statement text
        m_sql = re.search(r"```(?:sql)?\s*([\s\S]*?)\s*```", raw_text, re.IGNORECASE)
        repaired_sql = ""
        if m_sql:
            repaired_sql = m_sql.group(1).strip()
        else:
            m_raw = re.search(r"\b((?:SELECT|WITH|INSERT|UPDATE|DELETE|CREATE|ALTER|DROP)\b[\s\S]*?)(?:;|\n\n[A-Z]|$)", raw_text, re.IGNORECASE)
            if m_raw:
                repaired_sql = m_raw.group(1).strip()

        if not repaired_sql:
            return None, raw_text

        # Validate that repaired operation preserved the intended operation!
        repaired_op = detect_intended_operation(repaired_sql)
        if op != "UNKNOWN" and repaired_op != op:
            # Operation was altered (e.g. converted to SELECT), reject repair
            return None, f"Repair failed: model converted {op} to {repaired_op}"

        m_expl = re.search(r"explanation:\s*([\s\S]*)", raw_text, re.IGNORECASE)
        explanation = m_expl.group(1).strip() if m_expl else f"Repaired {op} statement."
        return repaired_sql, explanation
    except Exception as e:
        return None, f"Repair error: {e}"

