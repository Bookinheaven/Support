"""Natural-language to dialect-specific SQL query generator using LLM service."""

import json
import re
from typing import TYPE_CHECKING, Optional

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

    # 3. Fallback: Search for SELECT / WITH statement in plain text
    raw_query_match = re.search(r"\b(SELECT\b[\s\S]*?)(?:;|\n\n[A-Z]|$)", clean_text, re.IGNORECASE)
    if not raw_query_match:
        raw_query_match = re.search(r"\b(WITH\b[\s\S]*?)(?:;|\n\n[A-Z]|$)", clean_text, re.IGNORECASE)

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
        if state.cached_schema is None:
            try:
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
        if not schema or not schema.tables:
            return QueryDraft(
                question=question,
                sql="",
                explanation="The active database contains no accessible tables or schema elements.",
                dialect=dialect_name,
                validation_status="Rejected: Empty database schema",
                is_validated=False,
                is_parsed=False,
                provider=state.active_provider,
                model=state.active_model,
            )

        # 2. Extract deterministic relevant schema context with join path preservation
        schema_context, is_full_cov, coverage_summary, selected_tables = get_relevant_schema_context(
            schema, question=question, max_tables=12
        )

        # 3. Build bounded conversation history (last 2-3 question/draft pairs)
        history_context = []
        for msg in state.history[-6:]:
            if msg.role == "user":
                history_context.append(f"User Question: {msg.content}")
            elif msg.role == "assistant" and msg.metadata.get("sql"):
                history_context.append(f"Previous SQL Draft: {msg.metadata['sql']}")

        history_prompt = ""
        if history_context:
            history_prompt = "Relevant Conversation History:\n" + "\n".join(history_context) + "\n\n"

        # 4. Construct prompt with explicit dialect rules
        dialect_hints = {
            Dialect.POSTGRES: "Use PostgreSQL syntax. Use LIMIT for row limits. Use EXTRACT(field FROM col) for date parts.",
            Dialect.MYSQL: "Use MySQL syntax. Use LIMIT for row limits. Use backticks for reserved names.",
            Dialect.SQLITE: "Use SQLite syntax. Use LIMIT for row limits. Use strftime() for date formatting.",
            Dialect.MSSQL: "Use Microsoft SQL Server (T-SQL) syntax. Use TOP (n) or OFFSET ... FETCH for row limits.",
        }
        dialect_hint = dialect_hints.get(profile.dialect, "Use standard ANSI SQL.")

        system_instructions = (
            f"You are an expert SQL query generator for {dialect_name.upper()}.\n"
            f"Your task is to convert the user's natural language question into a single, correct, read-only SQL query.\n\n"
            f"DIALECT RULES:\n"
            f"- Target dialect: {dialect_name.upper()}\n"
            f"- {dialect_hint}\n\n"
            f"CRITICAL SAFETY & CORRECTNESS CONSTRAINTS:\n"
            f"1. Generate ONLY read-only queries (SELECT or WITH ... SELECT).\n"
            f"2. NEVER generate INSERT, UPDATE, DELETE, DROP, CREATE, ALTER, TRUNCATE, or multi-statement queries.\n"
            f"3. NEVER use table-creation or file export clauses (e.g. SELECT INTO, INTO OUTFILE).\n"
            f"4. NEVER use locking clauses or locking hints (e.g. FOR UPDATE, FOR SHARE, UPDLOCK, HOLDLOCK).\n"
            f"5. Use ONLY the table and column names defined in the schema below. NEVER invent columns or tables.\n"
            f"6. Prefer explicit column lists instead of SELECT * when practical.\n"
            f"7. Include a reasonable limit (e.g. LIMIT 10 or TOP (10)) when returning ranked, top, or large result sets.\n"
            f"8. If the request is ambiguous or the schema lacks required data, explain what is missing.\n"
            f"9. SENSITIVE DATA PROTECTION: NEVER select sensitive credential, password, secret, or token fields. Always prefer explicit non-sensitive columns over SELECT *.\n\n"
            f"DATABASE SCHEMA:\n"
            f"{schema_context}\n\n"
            f"OUTPUT FORMAT:\n"
            f"Respond with a SQL code block followed by a brief explanation:\n"
            f"```sql\n"
            f"SELECT ...\n"
            f"```\n"
            f"Explanation:\n"
            f"<concise description of how the query answers the user question>\n"
        )

        full_prompt = f"{system_instructions}\n{history_prompt}Current User Question: {question}"

        # 5. Invoke LLM Gateway
        target_provider = state.active_provider
        target_model = state.active_model
        curr_db_id = state.db_check_id

        try:
            resp = self.llm_service.generate(
                prompt=full_prompt,
                provider=target_provider,
                model=target_model,
                settings=GenerationSettings(temperature=0.1, timeout=30.0),
            )
            raw_text = resp.text
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
        if not sql:
            is_valid = False
            validation_status = "Rejected: No SQL query generated"
            is_parsed = False
            schema_status = "Not checked"
            is_schema_ver = None
            tables_used = []
            unknown_tables = []
            unknown_cols = []
            if clarification:
                explanation = clarification
        else:
            val_res = validate_sql_query(sql, dialect=profile.dialect, schema=schema)
            is_valid = val_res.is_valid
            validation_status = val_res.status_message
            is_parsed = val_res.is_parsed
            schema_status = val_res.schema_status
            is_schema_ver = val_res.is_schema_verified
            tables_used = val_res.tables_referenced
            unknown_tables = val_res.unknown_tables
            unknown_cols = val_res.unknown_columns

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
            provider=target_provider,
            model=target_model,
            clarification=clarification,
            tables_used=tables_used,
            unknown_tables=unknown_tables,
            unknown_columns=unknown_cols,
            schema_coverage=coverage_summary,
            raw_response=raw_text,
        )
