"""Direct LLM-driven interpretation and operation selection for database requests."""

import json
import re
from typing import Optional

from support.core.dialect import Dialect
from support.llm.models import GenerationSettings
from support.llm.service import LLMService
from support.mutation.planner import resolve_mutation_target_table
from support.schema.models import DatabaseSchema
from support.tools.models import ToolCall
from support.tools.registry import DATABASE_TOOLS, get_available_tools
from support.workspace.state import WorkspaceSessionState


def _is_sample_data_authorized(text: str) -> bool:
    """Helper to detect if text authorizes generating sample/dummy data."""
    norm = text.lower()
    return bool(
        re.search(
            r"\b(?:sample|random|dummy|fake|whatever|any)\s*(?:data|details|values|fields|information|info)?\b",
            norm,
        )
    )


def select_tool_for_user_request(
    message: str,
    schema: Optional[DatabaseSchema] = None,
    profile: Optional[object] = None,
    state: Optional[WorkspaceSessionState] = None,
    llm_service: Optional[LLMService] = None,
) -> ToolCall:
    """Interpret natural-language user requests using LLM with context and schema."""
    clean = message.strip()
    if schema is None and state and state.cached_schema:
        schema = state.cached_schema
    if profile is None and state and state.database_profile:
        profile = state.database_profile

    # If LLM service is available, let the model freely interpret request
    if llm_service:
        try:
            # 1. Tools descriptions
            tools_desc = []
            for t in get_available_tools():
                tools_desc.append(
                    f"- Tool '{t.name}': {t.description}\n  Parameters: {json.dumps(t.parameters)}"
                )
            tools_prompt = "\n".join(tools_desc)

            # 2. Database dialect and capabilities
            db_name = getattr(profile, "name", None) or (state.active_database if state else "unknown")
            dialect_val = getattr(getattr(profile, "dialect", None), "value", "relational SQL")
            db_context = f"Active Database: '{db_name}' (Dialect: {dialect_val})"

            # 3. Discovered Schema Context (efficient subset)
            schema_summary = "No discovered schema available."
            if schema and schema.tables:
                tables_info = []
                for t_name, t in list(schema.tables.items())[:25]:
                    cols = ", ".join(f"{c.name} ({c.data_type})" for c in t.columns[:8])
                    tables_info.append(f"  * {t.full_name}: {cols}")
                schema_summary = "Discovered Database Tables:\n" + "\n".join(tables_info)

            # 4. Recent Conversation History
            history_summary = ""
            state_history = getattr(state, "history", None) or getattr(state, "messages", None)
            if state and state_history:
                recent_msgs = state_history[-6:]
                h_lines = [f"{m.role.capitalize()}: {m.content[:200]}" for m in recent_msgs]
                history_summary = "Recent Conversation History:\n" + "\n".join(h_lines)

            # 5. Active Pending Operation Context
            pending_context = ""
            if state and getattr(state, "pending_mutation", None):
                pending = state.pending_mutation
                pending_context = (
                    "Active Pending Operation Awaiting Input:\n"
                    f"  Operation: {pending.operation.value} on table '{pending.table_name}'\n"
                    f"  Current values: {json.dumps(pending.values)}\n"
                    f"  Missing required fields: {', '.join(pending.missing_fields)}\n"
                    f"  Allow sample data: {pending.allow_sample_data}\n"
                    f"  Original question: {pending.question}\n"
                )

            system_prompt = (
                "You are Support, an intelligent AI database exploration and query assistant.\n"
                "Your job is to understand the user's natural-language request and choose the appropriate tool.\n\n"
                f"{db_context}\n\n"
                f"{schema_summary}\n\n"
                f"{history_summary}\n\n"
                f"{pending_context}\n\n"
                f"Available Tools:\n{tools_prompt}\n\n"
                "Instructions:\n"
                "- Understand the user's natural language freely, tolerating typos, conversational phrasing, and follow-ups.\n"
                "- If the user wants to retrieve data, analyze records, view rows, or query a table (e.g. 'show everything in orders items table', 'show users', 'select all records'), choose 'query_database'.\n"
                "- If the user wants to create or define a new table, choose 'create_table', providing 'table_name' and the full dialect 'ddl_sql'.\n"
                "- If the user wants to alter an existing table, choose 'alter_table', providing 'table_name' and the full dialect 'ddl_sql'.\n"
                "- If the user wants to create an index, choose 'create_index', providing 'table_name' and the full dialect 'ddl_sql'.\n"
                "- If the user wants to add/insert a record into an existing table (e.g. 'add a user in users table'), choose 'create_record'. If user authorizes random or sample data, set allow_sample_data: true.\n"
                "- If the user wants to update or delete records, choose 'update_records' or 'delete_records' with filter conditions.\n"
                "- If the user is modifying a pending operation, update the pending operation's arguments.\n"
                "- If the user wants to inspect row data across tables, choose 'inspect_tables'.\n"
                "- If the user strictly wants to enumerate table names in the database schema without row contents, choose 'list_tables'.\n"
                "- If the user wants an ER diagram, choose 'generate_diagram'.\n"
                "- If the user's instruction is ambiguous, incomplete, or requires clarification, choose 'ask_clarification'.\n"
                "Respond ONLY with a valid JSON object matching:\n"
                "{\n"
                '  "tool": "<tool_name>",\n'
                '  "arguments": { ... },\n'
                '  "explanation": "<brief reason>"\n'
                "}"
            )

            user_prompt = f"User Request: {clean}"

            target_provider = state.active_provider if state else None
            target_model = state.active_model if state else None

            full_tool_prompt = f"{system_prompt}\n\n{user_prompt}"
            from support.core.performance import get_current_tracker
            import time

            tracker = get_current_tracker()
            t0 = time.perf_counter()
            resp = llm_service.generate(
                prompt=full_tool_prompt,
                provider=target_provider,
                model=target_model,
                settings=GenerationSettings(temperature=0.0, timeout=12.0),
            )
            dur_ms = (time.perf_counter() - t0) * 1000.0

            if tracker:
                usage = getattr(resp, "usage", None)
                p_tok = getattr(usage, "prompt_tokens", None) if usage else None
                c_tok = getattr(usage, "completion_tokens", None) if usage else None
                tot_tok = getattr(usage, "total_tokens", None) if usage else None
                tracker.record_llm_call(
                    purpose="tool_selection",
                    duration_ms=dur_ms,
                    provider=target_provider or getattr(resp, "provider", ""),
                    model=target_model or getattr(resp, "model", ""),
                    prompt_tokens=p_tok,
                    completion_tokens=c_tok,
                    total_tokens=tot_tok,
                    prompt_chars=len(full_tool_prompt),
                    finish_reason=getattr(resp, "finish_reason", None),
                )

            raw = resp.text.strip()
            m_json = re.search(r"\{[\s\S]*\}", raw)
            if m_json:
                data = json.loads(m_json.group(0))
                tool_name = data.get("tool") or data.get("action") or data.get("name") or ""
                if tool_name in DATABASE_TOOLS:
                    args = data.get("arguments") or data.get("parameters") or data.get("args") or {}
                    if not isinstance(args, dict):
                        args = {}
                    return ToolCall(
                        tool=tool_name,
                        arguments=args,
                        explanation=data.get("explanation"),
                    )
        except Exception:
            pass

    # Resilient fallback when LLM is unavailable or unconfigured
    return _simple_fallback(clean, schema, state)


def _simple_fallback(
    clean: str,
    schema: Optional[DatabaseSchema],
    state: Optional[WorkspaceSessionState] = None,
) -> ToolCall:
    """Minimal, safe fallback when LLM service is offline or unconfigured."""
    norm = clean.lower()

    # Ambiguous or very short inputs
    if norm in ("?", "what", "what?", "hi", "hello", "hey", "test", "ok", "yes", "no") or len(clean) < 3:
        return ToolCall(
            tool="ask_clarification",
            arguments={"question": "Could you please clarify what database operation you would like to perform?"},
        )

    # ER Diagram
    if any(k in norm for k in ("er diagram", "erd", "diagram", "relationships")):
        found_tables = []
        if schema and schema.tables:
            for t_name in schema.tables:
                if t_name.lower() in norm:
                    found_tables.append(t_name)
        if not found_tables:
            clean_norm = clean.rstrip(".?!; \t\n")
            m_diag = re.search(r"\b(?:of|for)\s+(?:the\s+)?(.+?)(?:\s+tables?)?$", clean_norm, re.IGNORECASE)
            if m_diag:
                raw_chunk = m_diag.group(1).replace(" and ", ",")
                raw_tokens = [t.strip() for t in raw_chunk.split(",") if t.strip()]
                ignore = {"the", "a", "an", "all", "schema", "database", "tables", "table", "er", "erd"}
                for tok in raw_tokens:
                    tok_words = [w for w in tok.split() if w.lower() not in ignore]
                    for tw in tok_words:
                        if tw.lower() not in ignore and tw not in found_tables:
                            found_tables.append(tw)
        return ToolCall(tool="generate_diagram", arguments={"tables": found_tables})

    # DDL Table creation
    is_diagram_req = any(k in norm for k in ("diagram", "erd", "relationship"))
    if not is_diagram_req and (any(k in norm for k in ("create table", "make a table", "new table", "create a table", "make me a table")) or (("create" in norm or "make" in norm or "creat" in norm) and ("table" in norm or "tabel" in norm))):
        words = [w.strip(".,;:'\"") for w in clean.split()]
        table_name = "example_table"
        skip = {"called", "named", "for", "storing", "a", "an", "the", "with", "table", "tabel", "me"}
        for idx, w in enumerate(words):
            if w.lower() in ("table", "tabel"):
                for next_w in words[idx + 1:]:
                    if next_w.lower() not in skip:
                        table_name = next_w
                        break
                break

        cols = ""
        m_cols = re.search(r"\bwith\s+([^\.]+)", clean, re.IGNORECASE)
        if m_cols:
            cols = m_cols.group(1).strip()
        return ToolCall(
            tool="create_table",
            arguments={"table_name": table_name, "columns_description": cols},
        )

    # Alter table
    if "alter table" in norm or ("add" in norm and "column" in norm):
        tbl_name = ""
        if schema:
            resolved, _, _ = resolve_mutation_target_table(clean, schema)
            if resolved:
                tbl_name = resolved
        if not tbl_name:
            m_tbl = re.search(r"\b(?:to|in|on)\s+([a-zA-Z0-9_]+)\b", clean, re.IGNORECASE)
            if m_tbl:
                tbl_name = m_tbl.group(1)
        col_def = clean
        m_col = re.search(r"\badd\s+(?:an?\s+)?([a-zA-Z0-9_]+(?:\s+[a-zA-Z0-9_\(\)]+)?)\s+column\b", clean, re.IGNORECASE)
        if m_col:
            col_def = m_col.group(1)
        elif "column" in clean.lower():
            m_col2 = re.search(r"\bcolumn\s+([a-zA-Z0-9_]+(?:\s+[a-zA-Z0-9_\(\)]+)?)", clean, re.IGNORECASE)
            if m_col2:
                col_def = m_col2.group(1)
        return ToolCall(
            tool="alter_table",
            arguments={"table_name": tbl_name, "action": "add_column", "column_definition": col_def},
        )

    # Create Index
    if "index" in norm and any(k in norm for k in ("create", "add", "make")):
        tbl_name = ""
        cols = []
        if schema:
            resolved, _, _ = resolve_mutation_target_table(clean, schema)
            if resolved:
                tbl_name = resolved
        if not tbl_name:
            m_tbl = re.search(r"\b(?:on|for|in)\s+([a-zA-Z0-9_]+)\b", clean, re.IGNORECASE)
            if m_tbl:
                tbl_name = m_tbl.group(1)
        m_cols = re.search(r"\(([^)]+)\)", clean)
        if m_cols:
            cols = [c.strip() for c in m_cols.group(1).split(",") if c.strip()]
        return ToolCall(
            tool="create_index",
            arguments={"table_name": tbl_name, "columns": cols, "unique": "unique" in norm},
        )

    # Insert / Add record
    if any(k in norm for k in ("add a user", "add user", "insert into", "insert a record", "add a record", "add a new")):
        sample_auth = _is_sample_data_authorized(clean)
        tbl_name = ""
        if schema:
            resolved, _, _ = resolve_mutation_target_table(clean, schema)
            if resolved:
                tbl_name = resolved
        return ToolCall(
            tool="create_record",
            arguments={"table_name": tbl_name, "allow_sample_data": sample_auth},
        )

    # Update records
    if any(k in norm for k in ("update the", "update users", "update records", "change")):
        tbl_name = ""
        if schema:
            resolved, _, _ = resolve_mutation_target_table(clean, schema)
            if resolved:
                tbl_name = resolved
        return ToolCall(
            tool="update_records",
            arguments={"table_name": tbl_name, "conditions": {}},
        )

    # Delete records
    if any(k in norm for k in ("delete", "remove from", "remove a record", "remove record")):
        tbl_name = ""
        if schema:
            resolved, _, _ = resolve_mutation_target_table(clean, schema)
            if resolved:
                tbl_name = resolved
        return ToolCall(
            tool="delete_records",
            arguments={"table_name": tbl_name, "conditions": {}},
        )


    # Multi-table row sampling inspection
    has_row_intent = any(k in norm for k in ("first row", "sample", "overview of the data", "records from", "rows from", "row from", "quick overview", "preview data", "print the first"))
    is_all_tables = any(k in norm for k in ("all tables", "every table", "each table", "all the tables"))
    if has_row_intent:
        found_tables = []
        if schema and schema.tables:
            for t_name, tbl_obj in schema.tables.items():
                short_name = getattr(tbl_obj, "name", t_name.split(".")[-1]).lower()
                full_name = t_name.lower()
                if (
                    re.search(r"\b" + re.escape(full_name) + r"\b", norm)
                    or re.search(r"\b" + re.escape(short_name) + r"\b", norm)
                ):
                    if t_name not in found_tables:
                        found_tables.append(t_name)
        if not found_tables:
            m_t = re.search(r"\b(?:from|in)\s+([a-zA-Z0-9_,\s]+?)(?:\s+tables?)?\s*$", clean, re.IGNORECASE)
            if m_t:
                raw = m_t.group(1).replace(" and ", ",").split(",")
                ignore = {"the", "a", "an", "all", "each", "every", "database", "tables", "table"}
                found_tables = [t.strip() for t in raw if t.strip() and t.strip().lower() not in ignore]

        limit = 1
        m_num = re.search(r"\b(?:first|top|sample|limit(?:\s+to)?)\s+(\d+|one|two|three|four|five|six|seven|eight|nine|ten)\b", norm)
        word_map = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10}
        if m_num:
            v = m_num.group(1)
            limit = int(v) if v.isdigit() else word_map.get(v, 1)
        elif re.search(r"\b(\d+)\s+rows?\b", norm):
            limit = int(re.search(r"\b(\d+)\s+rows?\b", norm).group(1))

        if len(found_tables) == 1 and not is_all_tables:
            return ToolCall(tool="query_database", arguments={"query": clean})
        elif len(found_tables) >= 2:
            return ToolCall(
                tool="inspect_tables",
                arguments={"all_tables": False, "tables": found_tables, "row_limit": limit},
            )
        elif is_all_tables or "table" in norm:
            return ToolCall(
                tool="inspect_tables",
                arguments={"all_tables": True, "tables": [], "row_limit": limit},
            )

    # Enumerate tables only when strictly asking for table names without querying data/rows
    strict_list_phrases = (
        "show tables",
        "list tables",
        "show all tables",
        "list all tables",
        "what tables",
        "what tables exist",
        "tables exist",
        "display all tables",
    )
    clean_norm = norm.strip(" .?!;:")
    if clean_norm in strict_list_phrases or (
        clean_norm.startswith(("show tables", "list tables", "show all tables", "list all tables"))
        and not any(k in norm for k in ("from", "in", "where", "with", "everything", "rows", "records", "data", "columns"))
    ):
        return ToolCall(tool="list_tables", arguments={})

    # Default to data retrieval query (let LLM decide the query)
    return ToolCall(tool="query_database", arguments={"query": clean})


# Alias for conversational interpretation
interpret_and_select_tool = select_tool_for_user_request

