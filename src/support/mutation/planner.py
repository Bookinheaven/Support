"""Schema-grounded change plan extraction, validation, and compilation for CRUD operations."""

from dataclasses import dataclass
import hashlib
import json
import re
import uuid
from typing import Any, Optional

from sqlalchemy import column, delete, insert, table, update
from sqlalchemy.sql import and_

from support.core.dialect import Dialect
from support.db.models import ConnectionProfile
from support.db.service import DatabaseService
from support.llm.models import GenerationSettings
from support.llm.service import LLMService
from support.mutation.models import ChangePlan, MutationType, WhereCondition
from support.query.privacy import is_sensitive_column_name
from support.schema.models import DatabaseSchema, TableSchema


@dataclass
class RawMutationProposal:
    """Unvalidated mutation proposal parsed from user request or model output."""

    operation: MutationType
    table_name: str
    values: dict[str, Any]
    conditions: dict[str, Any]
    inferred_reasons: dict[str, str]
    explanation: Optional[str] = None
    missing_fields: list[str] = None


def compute_plan_hash(
    database: str,
    dialect_val: str,
    operation_val: str,
    table_name: str,
    sql: str,
    params: dict[str, Any],
    matched_count: int,
) -> str:
    """Cryptographic SHA-256 fingerprint binding database, table, SQL, parameters, and matched records."""
    sorted_params = sorted((str(k), str(v)) for k, v in params.items())
    raw = f"{database}:{dialect_val}:{operation_val}:{table_name}:{sql}:{sorted_params}:{matched_count}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def resolve_mutation_target_table(
    question: str,
    schema: DatabaseSchema,
) -> tuple[Optional[str], Optional[TableSchema], Optional[str]]:
    """Resolve target table from user mutation request against active schema.

    Returns:
        (resolved_table_name, table_schema, error_message)
    """
    clean = question.strip()
    norm = clean.lower()

    # 1. Check explicit container/table syntax first (preposition phrases and table markers)
    explicit_patterns = [
        # "into [the] <tbl> [table]"
        r"\b(?:into)\s+(?:the\s+)?([a-zA-Z0-9_]+(?:\.[a-zA-Z0-9_]+)?)(?:\s+table)?\b",
        # "in [the] <tbl> table"
        r"\b(?:in)\s+(?:the\s+)?([a-zA-Z0-9_]+(?:\.[a-zA-Z0-9_]+)?)\s+table\b",
        # "to [the] <tbl> table"
        r"\b(?:to)\s+(?:the\s+)?([a-zA-Z0-9_]+(?:\.[a-zA-Z0-9_]+)?)\s+table\b",
        # "from [the] <tbl> [table]"
        r"\b(?:from)\s+(?:the\s+)?([a-zA-Z0-9_]+(?:\.[a-zA-Z0-9_]+)?)(?:\s+table)?\b",
        # "update [the] <tbl> [table]"
        r"\bupdate\s+(?:the\s+)?([a-zA-Z0-9_]+(?:\.[a-zA-Z0-9_]+)?)(?:\s+table)?\b",
        # "<tbl> table"
        r"\b([a-zA-Z0-9_]+(?:\.[a-zA-Z0-9_]+)?)\s+table\b",
        # "table <tbl>"
        r"\btable\s+([a-zA-Z0-9_]+(?:\.[a-zA-Z0-9_]+)?)\b",
    ]

    stop_words = {"a", "an", "the", "this", "that", "each", "every", "all", "new", "record", "row"}

    explicit_cands: list[str] = []
    for pat in explicit_patterns:
        for m in re.finditer(pat, clean, re.IGNORECASE):
            cand = m.group(1).strip()
            if cand.lower() not in stop_words and cand.lower() not in [c.lower() for c in explicit_cands]:
                explicit_cands.append(cand)

    for cand in explicit_cands:
        tbl_obj = schema.get_table(cand)
        if tbl_obj:
            return tbl_obj.name, tbl_obj, None

    if explicit_cands:
        first_cand = explicit_cands[0]
        return first_cand, None, None

    # 2. Check exact matches for schema table names anywhere in the query
    exact_matches: list[TableSchema] = []
    for tbl in schema.tables.values():
        full_pat = r"\b" + re.escape(tbl.full_name.lower()) + r"\b"
        name_pat = r"\b" + re.escape(tbl.name.lower()) + r"\b"
        if re.search(full_pat, norm) or re.search(name_pat, norm):
            if tbl not in exact_matches:
                exact_matches.append(tbl)

    if len(exact_matches) == 1:
        return exact_matches[0].name, exact_matches[0], None
    elif len(exact_matches) > 1:
        names = [t.full_name for t in exact_matches]
        return None, None, f"Ambiguous table reference. Multiple tables match: {', '.join(names)}. Please clarify which table to modify."

    # 3. Check singular / plural stem variations against schema tables
    stem_matches: list[TableSchema] = []
    for tbl in schema.tables.values():
        t_low = tbl.name.lower()
        sing = t_low[:-1] if t_low.endswith("s") else None
        if t_low.endswith("ies"):
            sing = t_low[:-3] + "y"

        if sing and len(sing) >= 3:
            if re.search(r"\b" + re.escape(sing) + r"\b", norm):
                if tbl not in stem_matches:
                    stem_matches.append(tbl)
        else:
            plur = t_low + "s"
            if re.search(r"\b" + re.escape(plur) + r"\b", norm):
                if tbl not in stem_matches:
                    stem_matches.append(tbl)

    if len(stem_matches) == 1:
        return stem_matches[0].name, stem_matches[0], None
    elif len(stem_matches) > 1:
        names = [t.full_name for t in stem_matches]
        return None, None, f"Ambiguous table reference. Multiple plausible tables found: {', '.join(names)}. Please clarify which table to modify."

    # 4. Fallback for requests like "add a widget" where widget is nonexistent
    m_noun = re.search(r"\b(?:add|insert|create|update|delete|modify)\s+(?:a\s+|an\s+|new\s+)?([a-zA-Z0-9_]+)\b", clean, re.IGNORECASE)
    if m_noun:
        noun = m_noun.group(1).strip()
        if noun.lower() not in stop_words and noun.lower() not in ("table", "record", "row", "item", "data"):
            tbl_obj = schema.get_table(noun)
            if tbl_obj:
                return tbl_obj.name, tbl_obj, None
            return noun, None, None

    return None, None, None


def _extract_deterministic_proposal(
    question: str,
    schema: DatabaseSchema,
) -> Optional[RawMutationProposal]:
    """Deterministically parse natural language mutation requests against discovered schema tables."""
    clean = question.strip()
    norm = clean.lower()

    # Determine operation
    op: Optional[MutationType] = None
    if re.search(r"\b(add|insert|create)\b", norm):
        op = MutationType.INSERT
    elif re.search(r"\b(update|change|modify|set|mark)\b", norm):
        op = MutationType.UPDATE
    elif re.search(r"\b(delete|remove)\b", norm):
        op = MutationType.DELETE

    if not op:
        return None

    matched_table, tbl_schema, err = resolve_mutation_target_table(clean, schema)
    if err:
        return RawMutationProposal(
            operation=op,
            table_name="",
            values={},
            conditions={},
            inferred_reasons={},
            explanation=err,
        )

    if not matched_table:
        return None

    values: dict[str, Any] = {}
    conditions: dict[str, Any] = {}
    inferred_reasons: dict[str, str] = {}

    # Extract conditions (especially for UPDATE and DELETE)
    # 1. Pattern: "id = 42" or "id=42"
    for m in re.finditer(r"\b([a-zA-Z0-9_]+)\s*=\s*(?:'([^']*)'|\"([^\"]*)\"|([0-9]+(?:\.[0-9]+)?)|([a-zA-Z0-9_]+))\b", clean):
        col_cand, val_sq, val_dq, val_num, val_word = m.groups()
        val = val_sq if val_sq is not None else (val_dq if val_dq is not None else (val_num if val_num is not None else val_word))
        # Determine if this assignment belongs to WHERE clause or SET clause
        start_idx = m.start()
        prefix = clean[:start_idx].lower()
        if "where" in prefix or op == MutationType.DELETE:
            conditions[col_cand] = int(val) if val.isdigit() else val
        elif "set" in prefix or op == MutationType.UPDATE:
            values[col_cand] = int(val) if val.isdigit() else val

    # 2. Pattern: "task 42" or "user 5" or "record 10" or "with id 42"
    m_id = re.search(r"\b(?:id\s*=?\s*|#|with\s+id\s+)?(\d+)\b", clean)
    if m_id and not conditions and (op in (MutationType.UPDATE, MutationType.DELETE)):
        # If table has an 'id' column or single primary key, bind it
        pk_col = tbl_schema.primary_key[0] if (tbl_schema and tbl_schema.primary_key) else "id"
        conditions[pk_col] = int(m_id.group(1))

    # Extract field values for INSERT / UPDATE:
    # Pattern: "with username alex, email alex@example.com"
    # Pattern: "named Alex" -> name='Alex' or full_name='Alex'
    m_named = re.search(r"\b(?:named|name\s+is)\s+([a-zA-Z0-9_]+)\b", clean, re.IGNORECASE)
    if m_named and tbl_schema:
        name_val = m_named.group(1)
        if tbl_schema.get_column("full_name"):
            values["full_name"] = name_val
        elif tbl_schema.get_column("name"):
            values["name"] = name_val

        if tbl_schema.get_column("username") and "username" not in values:
            values["username"] = name_val.lower()
            inferred_reasons["username"] = "inferred from name"

    m_email = re.search(r"\b(?:with\s+)?email\s+(?:is\s+|to\s+)?([a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+\.[a-zA-Z0-9-.]+)\b", clean, re.IGNORECASE)
    if m_email:
        values["email"] = m_email.group(1)

    stop_words = {"a", "an", "the", "and", "or", "is", "to", "with", "for", "in", "of", "new"}
    m_username = re.search(r"\b(?:with\s+)?username\s+(?:is\s+|to\s+|=)?\s*([a-zA-Z0-9_]+)\b", clean, re.IGNORECASE)
    if m_username:
        cand = m_username.group(1).lower()
        if cand not in stop_words:
            values["username"] = m_username.group(1)

    # Pattern: "Change task 42 to completed" or "mark task 42 as completed"
    m_status = re.search(r"\b(?:to|as)\s+([a-zA-Z0-9_]+)\s*$", clean.rstrip(".!?; "), re.IGNORECASE)
    if m_status and op == MutationType.UPDATE:
        stat_cand = m_status.group(1).lower()
        if stat_cand in ("completed", "done", "pending", "active", "archived", "closed", "in_progress", "shipped", "cancelled"):
            if tbl_schema and tbl_schema.get_column("status"):
                values["status"] = stat_cand
            elif tbl_schema and tbl_schema.get_column("is_active"):
                values["is_active"] = stat_cand in ("completed", "done", "active")

    # If the user explicitly mentions column names to set without values:
    # e.g. "Add a new user with a username, email, and full name."
    missing_fields: list[str] = []
    if op == MutationType.INSERT and tbl_schema:
        for col in tbl_schema.columns:
            if col.name not in values and re.search(r"\b" + re.escape(col.name.lower()) + r"\b", norm):
                missing_fields.append(col.name)

    return RawMutationProposal(
        operation=op,
        table_name=matched_table,
        values=values,
        conditions=conditions,
        inferred_reasons=inferred_reasons,
        missing_fields=missing_fields,
    )


def _extract_llm_proposal(
    question: str,
    schema: DatabaseSchema,
    profile: ConnectionProfile,
    llm_service: LLMService,
    target_provider: str,
    target_model: str,
) -> Optional[RawMutationProposal]:
    """Use configured LLM with strict JSON schema to interpret complex mutation intent."""
    table_summaries = []
    for t_name, t in schema.tables.items():
        cols_desc = ", ".join(f"{c.name} ({c.data_type})" for c in t.columns)
        table_summaries.append(f"Table '{t_name}': {cols_desc}")

    prompt_schema = "\n".join(table_summaries)
    system_prompt = (
        "You are a database change planner. Analyze the user's data modification request against the database schema.\n"
        "Output ONLY a valid JSON object with the following schema:\n"
        "{\n"
        '  "operation": "INSERT" | "UPDATE" | "DELETE",\n'
        '  "table": "table_name",\n'
        '  "values": {"column_name": value},\n'
        '  "conditions": {"column_name": value},\n'
        '  "explanation": "brief explanation of intended change"\n'
        "}\n\n"
        "Rules:\n"
        "- Do NOT output SQL text. Only output the structured JSON object.\n"
        "- Match exact column names from the schema.\n"
        "- Never invent values for unspecified columns.\n"
        "- For UPDATE and DELETE, always specify appropriate conditions identifying the target records.\n"
        f"Available Schema:\n{prompt_schema}"
    )

    try:
        resp = llm_service.chat(
            provider=target_provider,
            model=target_model,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": question},
            ],
            settings=GenerationSettings(temperature=0.0),
        )
        content = resp.content.strip()
        m_json = re.search(r"\{[\s\S]*\}", content)
        if m_json:
            data = json.loads(m_json.group(0))
            op_str = str(data.get("operation", "")).upper()
            if op_str in ("INSERT", "UPDATE", "DELETE"):
                return RawMutationProposal(
                    operation=MutationType(op_str),
                    table_name=data.get("table", ""),
                    values=data.get("values", {}),
                    conditions=data.get("conditions", {}),
                    inferred_reasons={},
                    explanation=data.get("explanation"),
                )
    except Exception:
        pass

    return None


def propose_mutation_plan(
    question: str,
    schema: DatabaseSchema,
    profile: ConnectionProfile,
    llm_service: Optional[LLMService] = None,
    target_provider: Optional[str] = None,
    target_model: Optional[str] = None,
) -> Optional[RawMutationProposal]:
    """Parse mutation proposal using deterministic extractor first, falling back to LLM JSON extraction."""
    proposal = _extract_deterministic_proposal(question, schema)
    if proposal and proposal.table_name:
        # If deterministic found table and either values/conditions or explicit missing fields, use it
        if proposal.values or proposal.conditions or proposal.missing_fields:
            return proposal

    # Fallback to LLM if services provided
    if llm_service and target_provider and target_model:
        llm_prop = _extract_llm_proposal(
            question=question,
            schema=schema,
            profile=profile,
            llm_service=llm_service,
            target_provider=target_provider,
            target_model=target_model,
        )
        if llm_prop:
            return llm_prop

    return proposal


def build_change_plan(
    proposal: RawMutationProposal,
    schema: DatabaseSchema,
    profile: ConnectionProfile,
    preview_records: Optional[list[dict[str, Any]]] = None,
    matched_count: int = 0,
) -> ChangePlan:
    """Validate proposal against active schema and compile into parameterized SQLAlchemy Core statement.

    Performs:
    - Table and column validation.
    - Required column verification vs database defaults.
    - Type casting and parameter binding.
    - Prohibition of unconditional UPDATE / DELETE.
    - Foreign key relationship warnings for DELETE.
    - Cryptographic SHA-256 plan hash computation.
    """
    plan_id = str(uuid.uuid4())[:8]

    # 1. Validate Table Exists
    tbl_schema = schema.get_table(proposal.table_name)
    if not tbl_schema:
        return ChangePlan(
            plan_id=plan_id,
            operation=proposal.operation,
            database=profile.name,
            dialect=profile.dialect.value,
            table_name=proposal.table_name,
            is_valid=False,
            validation_error=f"Table '{proposal.table_name}' does not exist in database '{profile.name}'.",
        )

    canonical_table = tbl_schema.name
    fields_to_set: dict[str, Any] = {}
    inferred_fields: dict[str, str] = dict(proposal.inferred_reasons)
    default_fields: list[str] = []
    missing_required: list[str] = []

    # 2. Validate Fields to Set
    for col_cand, raw_val in proposal.values.items():
        col_obj = tbl_schema.get_column(col_cand)
        if not col_obj:
            return ChangePlan(
                plan_id=plan_id,
                operation=proposal.operation,
                database=profile.name,
                dialect=profile.dialect.value,
                table_name=canonical_table,
                is_valid=False,
                validation_error=f"Column '{col_cand}' does not exist in table '{canonical_table}'.",
            )
        # Type-aware validation / casting
        col_type = col_obj.data_type.upper()
        clean_val = raw_val
        if any(t in col_type for t in ("INT", "SERIAL", "BIGINT", "SMALLINT")):
            try:
                clean_val = int(raw_val)
            except (ValueError, TypeError):
                return ChangePlan(
                    plan_id=plan_id,
                    operation=proposal.operation,
                    database=profile.name,
                    dialect=profile.dialect.value,
                    table_name=canonical_table,
                    is_valid=False,
                    validation_error=f"Invalid value for integer column '{col_obj.name}': '{raw_val}'.",
                )
        elif "BOOL" in col_type:
            if isinstance(raw_val, str):
                clean_val = raw_val.strip().lower() in ("true", "1", "yes", "t")

        fields_to_set[col_obj.name] = clean_val

    # 3. For INSERT: Check required columns vs database defaults
    if proposal.operation == MutationType.INSERT:
        for col in tbl_schema.columns:
            if col.name in fields_to_set:
                continue

            # Determine if column is auto-generated primary key or has default
            is_auto_pk = col.is_primary_key and any(
                t in col.data_type.upper() for t in ("INT", "SERIAL", "AUTO", "IDENTITY")
            )
            has_default = col.default_value is not None

            if is_auto_pk:
                default_fields.append(f"{col.name} (auto-generated primary key)")
            elif has_default:
                default_fields.append(f"{col.name} (default: {col.default_value})")
            elif col.nullable:
                default_fields.append(f"{col.name} (nullable: set to NULL)")
            else:
                # Strictly required field missing!
                missing_required.append(col.name)

        # If user explicitly asked for fields but didn't provide values
        if proposal.missing_fields:
            for mf in proposal.missing_fields:
                if mf not in fields_to_set and mf not in missing_required:
                    missing_required.append(mf)

        if missing_required:
            missing_str = ", ".join(missing_required)
            return ChangePlan(
                plan_id=plan_id,
                operation=proposal.operation,
                database=profile.name,
                dialect=profile.dialect.value,
                table_name=canonical_table,
                fields_to_set=fields_to_set,
                default_fields=default_fields,
                missing_required_fields=missing_required,
                is_valid=False,
                validation_error=f"Required field(s) missing for '{canonical_table}': {missing_str}.",
            )

        if not fields_to_set:
            return ChangePlan(
                plan_id=plan_id,
                operation=proposal.operation,
                database=profile.name,
                dialect=profile.dialect.value,
                table_name=canonical_table,
                is_valid=False,
                validation_error=f"No fields specified for INSERT into '{canonical_table}'.",
            )

    # 4. For UPDATE and DELETE: Check selection conditions (MANDATORY!)
    conditions: list[WhereCondition] = []
    if proposal.operation in (MutationType.UPDATE, MutationType.DELETE):
        if not proposal.conditions:
            return ChangePlan(
                plan_id=plan_id,
                operation=proposal.operation,
                database=profile.name,
                dialect=profile.dialect.value,
                table_name=canonical_table,
                is_valid=False,
                validation_error=f"Rejected: Unconditional {proposal.operation.value} without a WHERE clause is prohibited.",
            )

        for col_cand, cond_val in proposal.conditions.items():
            col_obj = tbl_schema.get_column(col_cand)
            if not col_obj:
                return ChangePlan(
                    plan_id=plan_id,
                    operation=proposal.operation,
                    database=profile.name,
                    dialect=profile.dialect.value,
                    table_name=canonical_table,
                    is_valid=False,
                    validation_error=f"Condition column '{col_cand}' does not exist in table '{canonical_table}'.",
                )
            clean_cond_val = cond_val
            if any(t in col_obj.data_type.upper() for t in ("INT", "SERIAL", "BIGINT")):
                try:
                    clean_cond_val = int(cond_val)
                except (ValueError, TypeError):
                    pass
            conditions.append(WhereCondition(column=col_obj.name, operator="=", value=clean_cond_val))

    # 5. For DELETE: Foreign key cascade/restrict warnings
    fk_warnings: list[str] = []
    if proposal.operation == MutationType.DELETE:
        for other_tbl_name, other_tbl in schema.tables.items():
            for fk in other_tbl.foreign_keys:
                if fk.referred_table.lower() == canonical_table.lower():
                    fk_action = f" ({fk.on_delete})" if fk.on_delete else ""
                    fk_warnings.append(
                        f"Table '{other_tbl.name}' references '{canonical_table}' via foreign key {fk.display_str}{fk_action}."
                    )

    # 6. Compile Parameterized SQL using SQLAlchemy Core
    all_col_names = set(fields_to_set.keys()) | {c.column for c in conditions}
    sa_cols = [column(cn) for cn in all_col_names]
    sa_tbl = table(canonical_table, *sa_cols)

    params: dict[str, Any] = {}
    if proposal.operation == MutationType.INSERT:
        stmt = insert(sa_tbl).values(**fields_to_set)
        compiled = stmt.compile()
        compiled_sql = str(compiled)
        params = dict(compiled.params)
    elif proposal.operation == MutationType.UPDATE:
        where_clauses = [sa_tbl.c[cond.column] == cond.value for cond in conditions]
        stmt = update(sa_tbl).where(and_(*where_clauses)).values(**fields_to_set)
        compiled = stmt.compile()
        compiled_sql = str(compiled)
        params = dict(compiled.params)
    elif proposal.operation == MutationType.DELETE:
        where_clauses = [sa_tbl.c[cond.column] == cond.value for cond in conditions]
        stmt = delete(sa_tbl).where(and_(*where_clauses))
        compiled = stmt.compile()
        compiled_sql = str(compiled)
        params = dict(compiled.params)

    # Compute cryptographic plan hash
    plan_h = compute_plan_hash(
        database=profile.name,
        dialect_val=profile.dialect.value,
        operation_val=proposal.operation.value,
        table_name=canonical_table,
        sql=compiled_sql,
        params=params,
        matched_count=matched_count,
    )

    return ChangePlan(
        plan_id=plan_id,
        operation=proposal.operation,
        database=profile.name,
        dialect=profile.dialect.value,
        table_name=canonical_table,
        fields_to_set=fields_to_set,
        inferred_fields=inferred_fields,
        default_fields=default_fields,
        conditions=conditions,
        preview_records=preview_records or [],
        matched_record_count=matched_count,
        sql=compiled_sql,
        parameters=params,
        plan_hash=plan_h,
        is_valid=True,
        foreign_key_warnings=fk_warnings,
        user_explanation=proposal.explanation,
    )
