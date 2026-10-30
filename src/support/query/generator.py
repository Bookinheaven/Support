"""Natural-language to dialect-specific SQL query generator using LLM service."""

import json
import re
from typing import TYPE_CHECKING, Optional

from support.cli.display import console
from support.core.dialect import Dialect
from support.db.service import DatabaseService
from support.llm.models import GenerationSettings
from support.llm.service import LLMService
from support.query.models import QueryDraft
from support.query.schema_context import get_relevant_schema_context, inspect_database_schema
from support.query.validator import validate_sql_query

if TYPE_CHECKING:
    from support.workspace.state import WorkspaceSessionState


def _extract_sql_and_explanation(response_text: str) -> tuple[str, str, Optional[str]]:
    """Extract SQL query and explanation from model response text.

    Supports Markdown fenced code blocks, JSON responses, or raw text.
    """
    clean_text = response_text.strip()

    # 1. Check for JSON format
    if (clean_text.startswith("{") and clean_text.endswith("}")) or ("\"sql\"" in clean_text):
        try:
            # Look for JSON block in markdown or direct text
            json_match = re.search(r"\{.*\}", clean_text, re.DOTALL)
            if json_match:
                parsed = json.loads(json_match.group(0))
                sql = parsed.get("sql", "").strip()
                explanation = parsed.get("explanation", "").strip()
                clarification = parsed.get("clarification")
                if sql:
                    return sql, explanation, clarification
        except Exception:
            pass

    # 2. Check for fenced code block: ```sql ... ```
    sql_block_match = re.search(r"```(?:sql)?\s*([\s\S]*?)\s*```", clean_text, re.IGNORECASE)
    if sql_block_match:
        sql = sql_block_match.group(1).strip()
        # The explanation is whatever comes after or before the block
        parts = clean_text.split(sql_block_match.group(0))
        explanation_parts = [p.strip() for p in parts if p.strip()]
        explanation = "\n".join(explanation_parts)

        # Clean "Explanation:" label if present
        explanation = re.sub(r"^explanation:\s*", "", explanation, flags=re.IGNORECASE).strip()
        return sql, explanation or "Generated SQL query based on provided schema.", None

    # 3. Fallback: Search for any SQL statement in plain text
    raw_query_match = re.search(r"\b((?:SELECT|WITH|INSERT|UPDATE|DELETE|CREATE|ALTER|DROP)\b[\s\S]*?)(?:;|\n\n[A-Z]|$)", clean_text, re.IGNORECASE)

    if raw_query_match:
        sql = raw_query_match.group(1).strip()
        explanation = clean_text.replace(sql, "").strip()
        explanation = re.sub(r"^explanation:\s*", "", explanation, flags=re.IGNORECASE).strip()
        return sql, explanation or "Generated SQL query based on provided schema.", None

    # 4. If no query could be extracted (e.g. model asks for clarification)
    return "", clean_text, clean_text


class QueryGenerator:
    """Orchestrates natural language to validated SQL query generation."""

    def __init__(
        self,
        db_service: Optional[DatabaseService] = None,
        llm_service: Optional[LLMService] = None,
    ) -> None:
        self.db_service = db_service or DatabaseService()
        self.llm_service = llm_service or LLMService()

    def generate_draft(
        self,
        question: str,
        state: WorkspaceSessionState,
    ) -> QueryDraft:
        """Generate a validated SQL query draft for the active database.

        Args:
            question: The natural-language user query.
            state: The current interactive workspace session state.

        Returns:
            A QueryDraft containing the SQL, explanation, and validation status.
        """
        profile = state.database_profile
        if not profile:
            return QueryDraft(
                question=question,
                sql="",
                explanation="No active database selected. Use /db to select an active database.",
                dialect="unknown",
                validation_status="Rejected: No active database selected",
                is_validated=False,
            )

        dialect_name = profile.dialect.value

        # Non-relational check
        if profile.dialect == Dialect.MONGODB:
            return QueryDraft(
                question=question,
                sql="",
                explanation="MongoDB natural-language query generation is not supported yet.",
                dialect="mongodb",
                validation_status="Rejected: Dialect is MongoDB",
                is_validated=False,
            )

        # 1. Retrieve or reuse cached database schema
        from support.core.performance import get_current_tracker
        import time
        tracker = get_current_tracker()

        if state.cached_schema is None:
            try:
                if tracker:
                    with tracker.track("schema_retrieval"):
                        state.cached_schema = inspect_database_schema(profile, db_service=self.db_service)
                else:
                    state.cached_schema = inspect_database_schema(profile, db_service=self.db_service)
            except Exception as e:
                return QueryDraft(
                    question=question,
                    sql="",
                    explanation=f"Database schema inspection failed: {e}",
                    dialect=dialect_name,
                    validation_status=f"Rejected: Schema inspection error: {e}",
                    is_validated=False,
                    is_parsed=False,
                    provider=state.active_provider,
                    model=state.active_model,
                )

        schema = state.cached_schema
        t_pc0 = time.perf_counter()
        # 1.5 Extract referenced tables from the last two assistant SQL drafts using SQLGlot
        import sqlglot
        from sqlglot import exp

        conversational_tables: list[str] = []
        recent_draft_sqls: list[str] = []

        for msg in reversed(state.history):
            if msg.role == "assistant":
                draft_sql = msg.metadata.get("sql") or msg.metadata.get("draft_sql")
                if draft_sql and draft_sql.strip():
                    recent_draft_sqls.append(draft_sql.strip())
                    if len(recent_draft_sqls) >= 2:
                        break

        glot_dialect = profile.dialect.value if profile.dialect != Dialect.MSSQL else "tsql"
        for draft_sql in recent_draft_sqls:
            try:
                parsed = sqlglot.parse_one(draft_sql, read=glot_dialect)
                for tbl in parsed.find_all(exp.Table):
                    if tbl.name:
                        t_clean = tbl.name.lower()
                        if t_clean not in conversational_tables:
                            conversational_tables.append(t_clean)
            except Exception:
                found = re.findall(r"\b(?:FROM|JOIN|INTO|UPDATE)\s+([a-zA-Z0-9_\.]+)", draft_sql, re.IGNORECASE)
                for item in found:
                    t_clean = item.split(".")[-1].lower()
                    if t_clean not in conversational_tables:
                        conversational_tables.append(t_clean)

        if not schema or not schema.tables:
            schema_context = f"Database '{profile.name}' is currently empty (contains no tables yet)."
            is_full_cov = True
            coverage_summary = "Empty database schema"
            selected_tables = []
        else:
            # 2. Extract deterministic relevant schema context with join path & multi-turn preservation
            schema_context, is_full_cov, coverage_summary, selected_tables = get_relevant_schema_context(
                schema,
                question=question,
                max_tables=12,
                conversational_tables=conversational_tables,
            )

        # 3. Build bounded conversation history (last 2 question/draft pairs)
        history_pairs: list[tuple[str, Optional[str]]] = []
        curr_user_q: Optional[str] = None
        for msg in state.history[-8:]:
            if msg.role == "user":
                curr_user_q = msg.content
            elif msg.role == "assistant" and curr_user_q is not None:
                d_sql = msg.metadata.get("sql") or msg.metadata.get("draft_sql")
                if d_sql:
                    history_pairs.append((curr_user_q, d_sql))
                    curr_user_q = None

        recent_pairs = history_pairs[-2:]
        history_context = []
        for q, s in recent_pairs:
            history_context.append(f"User Question: {q}")
            history_context.append(f"Previous SQL Draft: {s}")

        history_prompt = ""
        if history_context:
            history_prompt = (
                "Relevant Conversation History:\n"
                + "\n".join(history_context)
                + "\nNote: If the current question is a follow-up to a previous question, adapt or modify the previous query draft.\n\n"
            )

        # 4. Construct prompt with explicit dialect rules
        dialect_hints = {
            Dialect.POSTGRES: "Use PostgreSQL syntax. Use LIMIT for row limits. Use EXTRACT(field FROM col) for date parts.",
            Dialect.MYSQL: "Use MySQL syntax. Use LIMIT for row limits. Use backticks for reserved names.",
            Dialect.SQLITE: "Use SQLite syntax. Use LIMIT for row limits. Use strftime() for date formatting.",
            Dialect.MSSQL: "Use Microsoft SQL Server (T-SQL) syntax. Use TOP (n) or OFFSET ... FETCH for row limits.",
        }
        dialect_hint = dialect_hints.get(profile.dialect, "Use standard ANSI SQL.")

        system_instructions = (
            f"You are an expert SQL generator for {dialect_name.upper()}.\n"
            f"Your task is to convert the user's natural language request into a single, correct SQL statement (SELECT, INSERT, UPDATE, DELETE, CREATE TABLE, ALTER TABLE, CREATE INDEX, etc.).\n\n"
            f"DIALECT RULES:\n"
            f"- Target dialect: {dialect_name.upper()}\n"
            f"- {dialect_hint}\n\n"
            f"RULES & CONSTRAINTS:\n"
            f"1. Generate a single, accurate SQL statement matching the user's intent.\n"
            f"2. For read requests, generate SELECT queries with explicit projections where practical.\n"
            f"3. For schema requests, generate clean DDL (CREATE TABLE, ALTER TABLE, CREATE INDEX) compatible with {dialect_name.upper()}.\n"
            f"4. For record modifications, generate parameterized or safe INSERT, UPDATE, or DELETE statements with explicit WHERE conditions.\n"
            f"5. SENSITIVE DATA PROTECTION: For SELECT queries, NEVER select sensitive credential, password, secret, or token fields. Prefer explicit non-sensitive columns over SELECT *.\n"
            f"6. NEVER generate multi-statement SQL.\n"
            f"7. TYPOS AND UNDERSPECIFIED REQUESTS:\n"
            f"   - Freely interpret the overall intent of the user's message, tolerating misspellings and conversational phrasing.\n"
            f"   - NEVER create column names from command words, instruction words, or typos.\n"
            f"   - If the user asks to create a table without specifying columns, either ask for clarification on the columns, or propose a clean, standard schema appropriate for the entity.\n"
            f"8. If the request is ambiguous or requires clarification before proceeding, explain what is needed without inventing artificial structures.\n\n"
            f"DATABASE SCHEMA:\n"
            f"{schema_context}\n\n"
            f"OUTPUT FORMAT:\n"
            f"Respond with a SQL code block followed by a brief explanation:\n"
            f"```sql\n"
            f"<SQL statement>\n"
            f"```\n"
            f"Explanation:\n"
            f"<concise description of how the SQL answers the user request>\n"
        )

        full_prompt = f"{system_instructions}\n{history_prompt}Current User Question: {question}"
        if tracker:
            tracker.stage_durations["prompt_construction"] = (
                tracker.stage_durations.get("prompt_construction", 0.0) + (time.perf_counter() - t_pc0) * 1000.0
            )

        # 5. Invoke LLM Gateway
        target_provider = state.active_provider
        target_model = state.active_model
        curr_db_id = state.db_check_id

        try:
            t_llm0 = time.perf_counter()
            with console.status(f"[cyan]Generating SQL draft ({dialect_name})...[/cyan]", spinner="dots"):
                resp = self.llm_service.generate(
                    prompt=full_prompt,
                    provider=target_provider,
                    model=target_model,
                    settings=GenerationSettings(temperature=0.1, timeout=30.0),
                )
            dur_ms = (time.perf_counter() - t_llm0) * 1000.0
            raw_text = resp.text

            if tracker:
                usage = getattr(resp, "usage", None)
                p_tok = getattr(usage, "prompt_tokens", None) if usage else None
                c_tok = getattr(usage, "completion_tokens", None) if usage else None
                tot_tok = getattr(usage, "total_tokens", None) if usage else None
                tracker.record_llm_call(
                    purpose="sql_generation",
                    duration_ms=dur_ms,
                    provider=target_provider or getattr(resp, "provider", ""),
                    model=target_model or getattr(resp, "model", ""),
                    prompt_tokens=p_tok,
                    completion_tokens=c_tok,
                    total_tokens=tot_tok,
                    prompt_chars=len(full_prompt),
                    finish_reason=getattr(resp, "finish_reason", None),
                )
        except Exception as e:
            state.set_model_status(
                state.provider_status or "Available",
                generation_tested=True,
                generation_status=f"Failed: {e}",
            )
            return QueryDraft(
                question=question,
                sql="",
                explanation=f"Model generation error: {e}",
                dialect=dialect_name,
                validation_status=f"Rejected: Model error: {e}",
                is_validated=False,
                is_parsed=False,
                provider=target_provider,
                model=target_model,
                schema_coverage=coverage_summary,
            )

        # 6. Parse response
        sql, explanation, clarification = _extract_sql_and_explanation(raw_text)

        # 6.5 Deterministically expand wildcard projections on sensitive tables
        if sql and schema:
            import sqlglot
            from support.query.privacy import try_expand_wildcard_projections
            try:
                glot_dialect = profile.dialect.value
                parsed_sql = sqlglot.parse_one(sql, read=glot_dialect)
                expanded = try_expand_wildcard_projections(parsed_sql, schema=schema, dialect_str=glot_dialect)
                if expanded is not None:
                    sql = expanded.sql(dialect=glot_dialect)
            except Exception:
                pass

        # 7. Discard if active database switched during generation
        if curr_db_id != state.db_check_id or state.database_profile != profile:
            return QueryDraft(
                question=question,
                sql="",
                explanation="Active database was changed while query was generating.",
                dialect=dialect_name,
                validation_status="Rejected: Stale generation result",
                is_validated=False,
                is_parsed=False,
                raw_response=raw_text,
                schema_coverage=coverage_summary,
            )

        # 8. Validate SQL independently via AST and verify schema alignment
        t_val0 = time.perf_counter()
        if not sql:
            is_valid = False
            validation_status = "Rejected: No SQL query generated"
            is_parsed = False
            schema_status = "Not checked"
            is_schema_ver = None
            tables_used = []
            unknown_tables = []
            unknown_cols = []
            detected_operation = "SELECT"
            is_readonly = True
            if clarification:
                explanation = clarification
        else:
            val_res = validate_sql_query(sql, dialect=profile.dialect, schema=schema, allow_mutations=True)
            is_valid = val_res.is_valid
            validation_status = val_res.status_message
            is_parsed = val_res.is_parsed
            schema_status = val_res.schema_status
            is_schema_ver = val_res.is_schema_verified
            tables_used = val_res.tables_referenced
            unknown_tables = val_res.unknown_tables
            unknown_cols = val_res.unknown_columns
            detected_operation = val_res.operation
            is_readonly = val_res.is_readonly

        if tracker:
            tracker.stage_durations["sql_validation"] = (
                tracker.stage_durations.get("sql_validation", 0.0) + (time.perf_counter() - t_val0) * 1000.0
            )

        # 8.5 Bounded, schema-grounded pre-presentation correction (max 2 attempts)
        correction_notes = None
        repair_attempts = 0
        max_repairs = 2

        if (not is_valid or is_schema_ver is False or bool(unknown_tables) or bool(unknown_cols)) and tracker:
            tracker.record_validation_failure()

        while (
            (not is_valid or is_schema_ver is False or bool(unknown_tables) or bool(unknown_cols))
            and repair_attempts < max_repairs
            and schema
            and sql
        ):
            from support.query.repair import (
                find_unambiguous_schema_match,
                build_repair_prompt,
            )

            suggestions = []
            has_ambiguity = False

            # Check unknown tables
            for unk_tbl in unknown_tables:
                matched_name, is_unambiguous = find_unambiguous_schema_match(
                    unk_tbl, list(schema.tables.keys())
                )
                if not is_unambiguous:
                    has_ambiguity = True
                    break
                elif matched_name:
                    suggestions.append(f"Table '{unk_tbl}' does not exist in schema. Use '{matched_name}' instead.")

            # Check unknown columns
            if not has_ambiguity and unknown_cols:
                all_cols = []
                for t_name in tables_used:
                    tbl_obj = schema.tables.get(t_name)
                    if tbl_obj:
                        all_cols.extend([c.name for c in tbl_obj.columns])
                for unk_col in unknown_cols:
                    matched_col, is_col_unambiguous = find_unambiguous_schema_match(
                        unk_col, all_cols
                    )
                    if not is_col_unambiguous:
                        has_ambiguity = True
                        break
                    elif matched_col:
                        suggestions.append(f"Column '{unk_col}' does not exist. Use '{matched_col}' instead.")

            if has_ambiguity:
                explanation = (
                    "The request references an ambiguous identifier. "
                    "Multiple schema objects match; please clarify which table or column was intended."
                )
                break

            if not suggestions and "parse" not in validation_status.lower() and "syntax" not in validation_status.lower():
                break

            repair_attempts += 1
            if tracker:
                tracker.record_repair_attempt()
            repair_prompt = build_repair_prompt(
                original_question=question,
                failed_sql=sql,
                error_description=validation_status,
                dialect_name=dialect_name,
                schema_context=schema_context,
                intended_operation=detected_operation,
                suggestions=suggestions,
            )

            try:
                t_rep0 = time.perf_counter()
                repair_resp = self.llm_service.generate(
                    prompt=repair_prompt,
                    provider=target_provider,
                    model=target_model,
                    settings=GenerationSettings(temperature=0.1, timeout=30.0),
                )
                rep_dur = (time.perf_counter() - t_rep0) * 1000.0
                if tracker:
                    tracker.stage_durations["sql_repair"] = (
                        tracker.stage_durations.get("sql_repair", 0.0) + rep_dur
                    )
                    rep_usage = getattr(repair_resp, "usage", None)
                    tracker.record_llm_call(
                        purpose="pre_presentation_repair",
                        duration_ms=rep_dur,
                        provider=target_provider or getattr(repair_resp, "provider", ""),
                        model=target_model or getattr(repair_resp, "model", ""),
                        prompt_tokens=getattr(rep_usage, "prompt_tokens", None) if rep_usage else None,
                        completion_tokens=getattr(rep_usage, "completion_tokens", None) if rep_usage else None,
                        total_tokens=getattr(rep_usage, "total_tokens", None) if rep_usage else None,
                        prompt_chars=len(repair_prompt),
                        finish_reason=getattr(repair_resp, "finish_reason", None),
                    )
                rep_sql, rep_expl, _ = _extract_sql_and_explanation(repair_resp.text)
                if rep_sql:
                    import sqlglot
                    from support.query.privacy import try_expand_wildcard_projections
                    try:
                        p_sql = sqlglot.parse_one(rep_sql, read=profile.dialect.value)
                        exp_rep = try_expand_wildcard_projections(p_sql, schema=schema, dialect_str=profile.dialect.value)
                        if exp_rep is not None:
                            rep_sql = exp_rep.sql(dialect=profile.dialect.value)
                    except Exception:
                        pass

                    rep_val = validate_sql_query(
                        rep_sql,
                        dialect=profile.dialect,
                        schema=schema,
                        allow_mutations=True,
                        expected_operation=detected_operation,
                    )
                    if rep_val.is_valid and rep_val.is_schema_verified is True and not rep_val.unknown_tables:
                        sql = rep_sql
                        explanation = rep_expl or explanation
                        is_valid = True
                        validation_status = rep_val.status_message
                        is_parsed = rep_val.is_parsed
                        schema_status = rep_val.schema_status
                        is_schema_ver = rep_val.is_schema_verified
                        tables_used = rep_val.tables_referenced
                        unknown_tables = rep_val.unknown_tables
                        unknown_cols = rep_val.unknown_columns
                        detected_operation = rep_val.operation
                        is_readonly = rep_val.is_readonly
                        correction_notes = f"Schema-grounded correction applied (attempt {repair_attempts})"
                        break
                    else:
                        sql = rep_sql
                        is_valid = rep_val.is_valid
                        validation_status = rep_val.status_message
                        is_schema_ver = rep_val.is_schema_verified
                        tables_used = rep_val.tables_referenced
                        unknown_tables = rep_val.unknown_tables
                        unknown_cols = rep_val.unknown_columns
                        detected_operation = rep_val.operation
                        is_readonly = rep_val.is_readonly
            except Exception:
                break

        # 9. Update state model generation test lifecycle
        gen_status = "Passed" if is_valid else "Failed"
        state.set_model_status(
            state.provider_status or "Available",
            generation_tested=True,
            generation_status=gen_status,
        )

        return QueryDraft(
            question=question,
            sql=sql,
            explanation=explanation,
            dialect=dialect_name,
            validation_status=validation_status,
            is_validated=is_valid,
            is_parsed=is_parsed,
            schema_status=schema_status,
            is_schema_verified=is_schema_ver,
            operation=detected_operation,
            is_readonly=is_readonly,
            provider=target_provider,
            model=target_model,
            clarification=clarification,
            tables_used=tables_used,
            unknown_tables=unknown_tables,
            unknown_columns=unknown_cols,
            schema_coverage=coverage_summary,
            raw_response=raw_text,
            correction_notes=correction_notes,
            repair_attempts=repair_attempts,
        )

    def repair_execution_failure(
        self,
        failed_draft: QueryDraft,
        error_message: str,
        state: "WorkspaceSessionState",
    ) -> Optional[QueryDraft]:
        """Attempt bounded (1 attempt) schema-grounded repair for a query that failed execution."""
        from support.query.repair import (
            build_repair_prompt,
            find_unambiguous_schema_match,
            is_recoverable_execution_error,
        )

        if not is_recoverable_execution_error(error_message):
            return None

        if not state.database_profile:
            return None

        profile = state.database_profile
        schema = state.cached_schema
        if not schema:
            return None

        target_provider = state.active_provider
        target_model = state.active_model
        dialect_name = profile.dialect.value

        suggestions = []
        # Find potential table names mentioned in error text
        words = re.findall(r"['\"]?([a-zA-Z_][a-zA-Z0-9_]*)['\"]?", error_message)
        for w in words:
            matched_tbl, is_unamb = find_unambiguous_schema_match(w, list(schema.tables.keys()))
            if is_unamb and matched_tbl and matched_tbl != w:
                suggestions.append(f"Table '{w}' was not found. Use '{matched_tbl}' instead.")

        schema_context, _, coverage_summary, _ = get_relevant_schema_context(
            schema, question=failed_draft.question, max_tables=12
        )

        from support.query.repair import detect_intended_operation
        intended_op = getattr(failed_draft, "operation", None) or detect_intended_operation(failed_draft.sql, failed_draft.question)

        repair_prompt = build_repair_prompt(
            original_question=failed_draft.question,
            failed_sql=failed_draft.sql,
            error_description=f"Execution error: {error_message}",
            dialect_name=dialect_name,
            schema_context=schema_context,
            intended_operation=intended_op,
            suggestions=suggestions,
        )

        from support.core.performance import get_current_tracker
        import time

        tracker = get_current_tracker()
        if tracker:
            tracker.record_repair_attempt()

        try:
            t_post0 = time.perf_counter()
            repair_resp = self.llm_service.generate(
                prompt=repair_prompt,
                provider=target_provider,
                model=target_model,
                settings=GenerationSettings(temperature=0.1, timeout=30.0),
            )
            post_dur = (time.perf_counter() - t_post0) * 1000.0
            if tracker:
                tracker.stage_durations["sql_repair"] = (
                    tracker.stage_durations.get("sql_repair", 0.0) + post_dur
                )
                post_usage = getattr(repair_resp, "usage", None)
                tracker.record_llm_call(
                    purpose="post_execution_repair",
                    duration_ms=post_dur,
                    provider=target_provider or getattr(repair_resp, "provider", ""),
                    model=target_model or getattr(repair_resp, "model", ""),
                    prompt_tokens=getattr(post_usage, "prompt_tokens", None) if post_usage else None,
                    completion_tokens=getattr(post_usage, "completion_tokens", None) if post_usage else None,
                    total_tokens=getattr(post_usage, "total_tokens", None) if post_usage else None,
                    prompt_chars=len(repair_prompt),
                    finish_reason=getattr(repair_resp, "finish_reason", None),
                )
            rep_sql, rep_expl, _ = _extract_sql_and_explanation(repair_resp.text)
            if not rep_sql:
                return None

            import sqlglot
            from support.query.privacy import try_expand_wildcard_projections
            try:
                p_sql = sqlglot.parse_one(rep_sql, read=profile.dialect.value)
                exp_rep = try_expand_wildcard_projections(p_sql, schema=schema, dialect_str=profile.dialect.value)
                if exp_rep is not None:
                    rep_sql = exp_rep.sql(dialect=profile.dialect.value)
            except Exception:
                pass

            rep_val = validate_sql_query(
                rep_sql,
                dialect=profile.dialect,
                schema=schema,
                allow_mutations=True,
                expected_operation=intended_op,
            )
            if not rep_val.is_valid:
                return None

            return QueryDraft(
                question=failed_draft.question,
                sql=rep_sql,
                explanation=rep_expl or f"Corrected SQL to fix execution error: {error_message}",
                dialect=dialect_name,
                validation_status=rep_val.status_message,
                is_validated=rep_val.is_valid,
                is_parsed=rep_val.is_parsed,
                schema_status=rep_val.schema_status,
                is_schema_verified=rep_val.is_schema_verified,
                operation=rep_val.operation,
                is_readonly=rep_val.is_readonly,
                provider=target_provider,
                model=target_model,
                tables_used=rep_val.tables_referenced,
                unknown_tables=rep_val.unknown_tables,
                unknown_columns=rep_val.unknown_columns,
                schema_coverage=coverage_summary,
                raw_response=repair_resp.text,
                correction_notes=f"Post-execution correction: {error_message[:70]}",
                repair_attempts=1,
                execution_status="Not performed",
            )
        except Exception:
            return None
