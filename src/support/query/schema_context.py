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


_RELATIONSHIP_KEYWORDS = {
    "join", "with", "along", "across", "between", "related", "and", "per", "each",
    "belonging", "together", "for", "by", "grouped", "combining", "combined",
}


def _tokenize(text: str) -> set[str]:
    """Tokenize question text into normalized keyword stems."""
    tokens = re.findall(r"[a-zA-Z0-9_]+", text.lower())
    return {t for t in tokens if len(t) > 1 and t not in _STOP_WORDS}


def _has_relationship_intent(text: str) -> bool:
    """Check if the text contains explicit join or relationship keywords."""
    tokens = re.findall(r"[a-zA-Z0-9_]+", text.lower())
    return any(t in _RELATIONSHIP_KEYWORDS for t in tokens)


def get_relevant_tables(
    schema: DatabaseSchema,
    question: str,
    max_tables: int = 12,
    conversational_tables: Optional[list[str]] = None,
) -> list[TableSchema]:
    """Deterministically rank and filter tables relevant to the user query, preserving join paths."""
    all_tables = list(schema.tables.values())
    if len(all_tables) <= 3:
        return all_tables

    keywords = _tokenize(question)
    conv_targets = {t.strip().lower() for t in (conversational_tables or []) if t.strip()}

    scores: dict[str, int] = {t.name: 0 for t in all_tables}
    directly_matched_names: set[str] = set()

    # 1. Direct table, column, and conversational matching
    table_name_matches: set[str] = set()
    for table in all_tables:
        t_name_lower = table.name.lower()
        t_short = t_name_lower.split(".")[-1]

        # Conversational match from previous SQL drafts
        if t_name_lower in conv_targets or t_short in conv_targets:
            scores[table.name] += 12
            table_name_matches.add(table.name)
            directly_matched_names.add(table.name)

        # Direct table name matching
        if any(kw == t_short or kw == t_name_lower or kw in t_short or t_short in kw for kw in keywords):
            scores[table.name] += 10
            table_name_matches.add(table.name)
            directly_matched_names.add(table.name)

        # Column matching
        for col in table.columns:
            c_name_lower = col.name.lower()
            if any(kw == c_name_lower for kw in keywords):
                scores[table.name] += 3
                directly_matched_names.add(table.name)

    # Fallback for ambiguous or general questions where no tables were matched
    if not directly_matched_names:
        return all_tables[:max_tables]

    # Filter out accidental column matches on tables that have no FK connection to table_name_matches
    if table_name_matches:
        for t_name in list(directly_matched_names):
            if t_name not in table_name_matches:
                t_obj = next((t for t in all_tables if t.name == t_name), None)
                if t_obj:
                    # Check if t_obj has any inbound or outbound FK to any table in table_name_matches
                    has_outbound = any(
                        (fk.referred_table in table_name_matches or fk.referred_table in [tm.split(".")[-1] for tm in table_name_matches])
                        for fk in t_obj.foreign_keys
                    )
                    has_inbound = any(
                        any(
                            (fk.referred_table == t_obj.name or fk.referred_table == t_obj.name.split(".")[-1])
                            for fk in other_t.foreign_keys
                        )
                        for other_t in all_tables if other_t.name in table_name_matches
                    )
                    # Also check if it serves as a bridge or chain
                    if not has_outbound and not has_inbound:
                        scores[t_name] = 0
                        directly_matched_names.discard(t_name)

    # If all were discarded (rare), restore initial matches
    if not directly_matched_names:
        directly_matched_names = set(table_name_matches)

    # Check for pure single-table query (1 directly matched table, no conversational tables, no relationship intent)
    has_rel = _has_relationship_intent(question)
    if len(directly_matched_names) == 1 and not conv_targets and not has_rel:
        matched_tbl = [t for t in all_tables if t.name in directly_matched_names]
        if matched_tbl:
            return matched_tbl

    # 2. Foreign-key neighborhood expansion (both outbound and inbound)
    initially_matched = [t for t in all_tables if scores.get(t.name, 0) > 0]
    for table in initially_matched:
        # Outbound foreign keys: table -> target
        for fk in table.foreign_keys:
            for target_t in all_tables:
                if (
                    target_t.name == fk.referred_table
                    or target_t.name.split(".")[-1] == fk.referred_table
                ):
                    scores[target_t.name] += 4

        # Inbound foreign keys: other_table -> table
        for other_t in all_tables:
            for fk in other_t.foreign_keys:
                if (
                    fk.referred_table == table.name
                    or fk.referred_table == table.name.split(".")[-1]
                ):
                    scores[other_t.name] += 4

    # 3. Intermediate bridge tables connecting matched tables
    positively_scored = [t for t in all_tables if scores.get(t.name, 0) > 0]
    if len(positively_scored) >= 2:
        for bridge in all_tables:
            if scores.get(bridge.name, 0) > 0:
                continue
            linked_targets: set[str] = set()

            # Bridge has outbound FKs to scored tables
            for fk in bridge.foreign_keys:
                for candidate in positively_scored:
                    if candidate.name == fk.referred_table or candidate.name.split(".")[-1] == fk.referred_table:
                        linked_targets.add(candidate.name)

            # Bridge has inbound FKs from scored tables
            for candidate in positively_scored:
                for fk in candidate.foreign_keys:
                    if fk.referred_table == bridge.name or fk.referred_table == bridge.name.split(".")[-1]:
                        linked_targets.add(candidate.name)

            if len(linked_targets) >= 2:
                scores[bridge.name] += 5

    # Filter to positively scored tables, sort descending by score then name
    ranked = [t for t in all_tables if scores.get(t.name, 0) > 0]
    ranked.sort(key=lambda t: (scores.get(t.name, 0), t.name), reverse=True)
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
    conversational_tables: Optional[list[str]] = None,
) -> tuple[str, bool, str, list[str]]:
    """Retrieve and format relevant schema context for model prompting.

    Returns:
        (schema_context_text, is_full_coverage, coverage_summary, selected_table_names)
    """
    relevant_tables = get_relevant_tables(
        schema,
        question=question,
        max_tables=max_tables,
        conversational_tables=conversational_tables,
    )
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
