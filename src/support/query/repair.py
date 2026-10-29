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
    )
    return any(ind in low for ind in recoverable_indicators)


def build_repair_prompt(
    original_question: str,
    failed_sql: str,
    error_description: str,
    dialect_name: str,
    schema_context: str,
    suggestions: Optional[list[str]] = None,
) -> str:
    """Construct a sanitized, bounded prompt requesting SQL draft repair from LLM."""
    sanitized_error = re.sub(r"[\r\n]+", " ", error_description.strip())
    if len(sanitized_error) > 300:
        sanitized_error = sanitized_error[:297] + "..."

    hint_lines = []
    if suggestions:
        hint_lines.append("SCHEMA CORRECTION HINTS:")
        for s in suggestions:
            hint_lines.append(f"- {s}")

    hints_block = "\n".join(hint_lines) + "\n\n" if hint_lines else ""

    return (
        f"You are an expert SQL repair assistant for {dialect_name.upper()}.\n"
        f"An earlier generated SQL draft for the question below failed validation or execution.\n\n"
        f"CRITICAL RULES:\n"
        f"1. Generate ONLY a single read-only SELECT query.\n"
        f"2. Use ONLY table and column names that exist in the DATABASE SCHEMA below.\n"
        f"3. NEVER select sensitive credential, password, secret, or token fields.\n"
        f"4. Do NOT guess unknown tables. Adhere strictly to the provided schema.\n\n"
        f"{hints_block}"
        f"DATABASE SCHEMA:\n"
        f"{schema_context}\n\n"
        f"ORIGINAL QUESTION:\n"
        f"{original_question}\n\n"
        f"FAILED SQL DRAFT:\n"
        f"```sql\n"
        f"{failed_sql}\n"
        f"```\n\n"
        f"FAILURE REASON:\n"
        f"{sanitized_error}\n\n"
        f"OUTPUT FORMAT:\n"
        f"Respond with a single corrected SQL block followed by explanation:\n"
        f"```sql\n"
        f"SELECT ...\n"
        f"```\n"
        f"Explanation:\n"
        f"<concise description of the fix>\n"
    )
