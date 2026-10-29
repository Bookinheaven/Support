"""Schema context retrieval, foreign-key path preservation, and relevance filtering."""

import re
from typing import Optional

from support.db.models import ConnectionProfile
from support.db.service import DatabaseService
from support.query.privacy import is_sensitive_column_name
from support.schema.models import DatabaseSchema, TableSchema
from support.schema.sources.live_db import LiveDatabaseSchemaSource


_STOP_WORDS = {
    "a", "an", "the", "in", "on", "of", "for", "to", "at", "by", "from",
    "with", "is", "are", "was", "were", "what", "which", "who", "whom",
    "how", "many", "show", "list", "find", "get", "give", "me", "select",
    "display", "tell", "all", "each", "every", "most", "top", "best",
}


def _tokenize(text: str) -> set[str]:
    """Tokenize question text into normalized keyword stems."""
    tokens = re.findall(r"[a-zA-Z0-9_]+", text.lower())
    return {t for t in tokens if len(t) > 1 and t not in _STOP_WORDS}


def get_relevant_tables(
    schema: DatabaseSchema,
    question: str,
    max_tables: int = 12,
) -> list[TableSchema]:
    """Deterministically rank and filter tables relevant to the user query, preserving join paths."""
    all_tables = list(schema.tables.values())
    if len(all_tables) <= max_tables:
        return all_tables

    keywords = _tokenize(question)
    scores: dict[str, int] = {t.name: 0 for t in all_tables}

    # 1. Direct table & column matching
    for table in all_tables:
        t_name_lower = table.name.lower()
        if any(kw in t_name_lower or t_name_lower in kw for kw in keywords):
            scores[table.name] += 5

        for col in table.columns:
            c_name_lower = col.name.lower()
            if any(kw in c_name_lower or c_name_lower in kw for kw in keywords):
                scores[table.name] += 2

    # 2. Direct foreign key partner boost
    for table in all_tables:
        for fk in table.foreign_keys:
            if scores.get(table.name, 0) > 0:
                for target_t in all_tables:
                    if target_t.name == fk.referred_table or target_t.name.split(".")[-1] == fk.referred_table:
                        scores[target_t.name] += 3

    # 3. Preserve foreign-key join paths / bridge tables
    # If two tables A and B both have positive scores, identify any intermediate bridge table C
    # that links them (e.g. order_items bridging orders and products, or goals bridging notes and routines)
    positively_scored = [t for t in all_tables if scores.get(t.name, 0) > 0]
    if len(positively_scored) >= 2:
        for bridge in all_tables:
            if scores.get(bridge.name, 0) > 0:
                continue
            # Check if bridge has foreign keys to multiple scored tables
            linked_targets = set()
            for fk in bridge.foreign_keys:
                for candidate in positively_scored:
                    if candidate.name == fk.referred_table or candidate.name.split(".")[-1] == fk.referred_table:
                        linked_targets.add(candidate.name)
            if len(linked_targets) >= 2:
                scores[bridge.name] += 4

    # Sort descending by score, then alphabetically
    ranked = sorted(all_tables, key=lambda t: (scores.get(t.name, 0), t.name), reverse=True)
    return ranked[:max_tables]


def get_schema_coverage_info(
    schema: DatabaseSchema,
    selected_tables: list[TableSchema],
) -> tuple[bool, str]:
    """Return whether full schema was provided and a descriptive coverage summary."""
    total = len(schema.tables)
    selected = len(selected_tables)
    is_full = selected >= total
    summary = (
        f"Full schema ({total} tables)"
        if is_full
        else f"Relevant subset ({selected} of {total} tables)"
    )
    return is_full, summary


def format_schema_for_prompt(
    tables: list[TableSchema],
    is_full_coverage: bool = True,
    total_table_count: int = 0,
) -> str:
    """Format table definitions into compact, unambiguous, sanitized DDL-like schema text."""
    if not tables:
        return "-- No tables available in the active database schema."

    lines: list[str] = []
    if not is_full_coverage and total_table_count > len(tables):
        lines.append(
            f"-- NOTE: Relevant schema subset ({len(tables)} of {total_table_count} tables) provided based on query relevance.\n"
        )

    for table in tables:
        col_defs: list[str] = []
        for col in table.columns:
            parts = [f"  {col.name}", col.data_type or "VARCHAR"]
            if is_sensitive_column_name(col.name):
                parts.append("[SENSITIVE - DO NOT SELECT]")
            elif col.is_primary_key or col.name in table.primary_key:
                parts.append("PRIMARY KEY")
            elif col.nullable is False:
                parts.append("NOT NULL")
            col_defs.append(" ".join(parts))

        # Add Foreign Keys
        for fk in table.foreign_keys:
            c_cols = ", ".join(fk.constrained_columns)
            r_cols = ", ".join(fk.referred_columns)
            col_defs.append(f"  FOREIGN KEY ({c_cols}) REFERENCES {fk.referred_table}({r_cols})")

        table_body = ",\n".join(col_defs)
        lines.append(f"TABLE {table.name} (\n{table_body}\n);")

    return "\n\n".join(lines)


def get_relevant_schema_context(
    schema: DatabaseSchema,
    question: str,
    max_tables: int = 12,
) -> tuple[str, bool, str, list[str]]:
    """Retrieve and format relevant schema context for model prompting.

    Returns:
        (schema_context_text, is_full_coverage, coverage_summary, selected_table_names)
    """
    relevant_tables = get_relevant_tables(schema, question=question, max_tables=max_tables)
    is_full, summary = get_schema_coverage_info(schema, relevant_tables)
    text = format_schema_for_prompt(
        relevant_tables,
        is_full_coverage=is_full,
        total_table_count=len(schema.tables),
    )
    selected_names = [t.name for t in relevant_tables]
    return text, is_full, summary, selected_names


def inspect_database_schema(
    profile: ConnectionProfile,
    db_service: Optional[DatabaseService] = None,
) -> DatabaseSchema:
    """Retrieve normalized database schema using existing LiveDatabaseSchemaSource."""
    dbs = db_service or DatabaseService()
    password = None
    try:
        _, password = dbs.manager.get_profile_with_password(profile.name)
    except Exception:
        pass

    source = LiveDatabaseSchemaSource(profile, password=password)
    result = source.discover()
    return result.schema
