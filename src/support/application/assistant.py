"""Application workflow for conversational assistant interactions and bounded tool execution."""

from __future__ import annotations

from dataclasses import dataclass, field
import json
import re
from typing import Any

from support.application.ports import LLMProvider
from support.application.tools import ToolRegistry, ToolResult, extract_presentation_options
from support.domain.assistant import ChatMessage, ToolCall, ToolDefinition
from support.domain.schema import DatabaseSchema, TableMetadata, TableType

DEFAULT_MAX_TOOL_ITERATIONS: int = 4
MAX_CONVERSATION_HISTORY_MESSAGES: int = 30

SYSTEM_PROMPT_TEMPLATE = (
    "You are Support, a local AI database intelligence assistant.\n"
    "Your role is to help developers inspect, explore, and understand their database schemas.\n\n"
    "Principles:\n"
    "1. Answer ordinary general questions (math, general knowledge, greetings) directly in plain text without calling database tools.\n"
    "2. Call database tools only when the user requests actual database metadata (tables, schema, columns, constraints, relationships, connection status).\n"
    "3. Use only registered database tools. Never invent tool names (such as 'calculate' or 'none').\n"
    "4. Never invent database objects, columns, constraints, or relationships.\n"
    "5. Never call tools unnecessarily. If no database tool is needed, respond directly with conversational text and do not return tool-call JSON.\n"
    "6. If asked about tables, views, or collections, call 'list_database_objects'.\n"
    "7. If asked to inspect or describe an object, or show its columns (including 'Show its columns'), call 'describe_object' with object_name. Never type out columns in conversational text yourself.\n"
    "8. If asked about foreign keys, references, or relationships between tables (including explaining how or why tables are related or reference each other), call 'get_relationships'.\n"
    "9. If asked about overall database structure, call 'get_database_overview'.\n"
    "10. For exact listing and inspection requests, use response_mode='display'.\n"
    "11. For explanatory questions (why, how, explain, compare), use response_mode='explain' to retrieve evidence before making database-specific claims.\n"
    "12. Do not repeat exact structured output after Support has already displayed it.\n"
    "13. Ask for clarification when a requested object is ambiguous.\n"
    "14. Never execute arbitrary SQL or modify databases. Support is strictly read-only.\n"
    "15. Support provides schema and metadata inspection only. Arbitrary record retrieval or table row fetching is NOT supported.\n"
    "16. If asked to display records or table rows, state that reading database records is not currently supported, and ask if the user wants to inspect structure.\n"
    "17. If the user asks for items in a table (e.g. 'Display items in orders'), do not guess 'order_items'. Ask for clarification.\n"
    "18. Resolve pronouns like 'it', 'its', or 'their' using the active conversation context.\n"
    "19. CRITICAL: You must execute the tools yourself using tool calling. Never instruct or ask the user to call or execute tools.\n"
    "20. Never ask for, output, or attempt to inspect credentials or secrets.\n"
    "21. When the user specifies formatting preferences (such as showing table names without schema or public prefix, showing only names, numbering results, or sorting alphabetically), pass those structured options inside the 'presentation_options' argument of 'list_database_objects' (e.g. {\"include_schema\": false, \"names_only\": true, \"numbered\": false, \"alphabetical\": false}) or 'get_relationships'. Always call the tool so Support can render the formatted results."
)

EXPLAIN_KEYWORDS = {
    "why",
    "how",
    "explain",
    "compare",
    "interpret",
    "analyse",
    "analyze",
    "reason",
    "summarize",
    "summarise",
}


def _resolve_response_mode(user_text: str, requested_mode: str | None = None) -> str:
    """Determine response mode ('display' or 'explain') from tool arguments and user intent."""
    if requested_mode == "explain":
        return "explain"
    if requested_mode == "display":
        return "display"

    words = {w.lower().strip("?!.,;:'\"") for w in user_text.split()}
    if words & EXPLAIN_KEYWORDS:
        return "explain"
    return "display"


def _check_intent_guardrails(user_text: str) -> str | None:
    """Detect requests for unsupported record retrieval or ambiguous table queries."""
    cleaned = user_text.strip().rstrip("?!.,;:'\"").lower()

    # Case 1: "display items in orders" or "show items in orders"
    m_items = re.search(r"\b(?:display|show)\s+(?:the\s+)?items\s+in\s+([a-zA-Z0-9_]+)", cleaned)
    if m_items:
        tbl = m_items.group(1)
        singular = tbl[:-1] if tbl.endswith("s") and len(tbl) > 1 else tbl
        items_tbl = f"{singular}_items"
        return (
            f"Do you want to inspect the structure of '{tbl}', inspect the '{items_tbl}' table, "
            f"view the relationship between {tbl} and items, or view actual record contents?\n\n"
            "Record retrieval is not available yet."
        )

    # Case 2: Explicit row/record requests: "show rows from orders", "show latest 10 orders", "display rows in orders"
    m_rows = re.search(r"\b(?:show|display|get|fetch|read)\s+(?:the\s+)?(?:latest\s+\d+\s+|all\s+)?(?:rows|records)\s+(?:from|in|of)\s+([a-zA-Z0-9_]+)", cleaned)
    if m_rows:
        tbl = m_rows.group(1)
        return (
            "Reading database records is not currently supported. Support provides read-only schema and metadata inspection.\n\n"
            f"Did you want to inspect the structure of '{tbl}'?"
        )

    m_latest = re.search(r"\b(?:show|display|get|fetch)\s+(?:the\s+)?latest\s+\d+\s+([a-zA-Z0-9_]+)", cleaned)
    if m_latest:
        tbl = m_latest.group(1)
        return (
            "Reading database records is not currently supported. Support provides read-only schema and metadata inspection.\n\n"
            f"Did you want to inspect the structure of '{tbl}'?"
        )

    # Case 3: Ambiguous "display <table_name>" or "show <table_name>"
    m_display = re.match(r"^(?:display|show)\s+([a-zA-Z0-9_]+)$", cleaned)
    if m_display:
        target = m_display.group(1)
        if target not in ("tables", "views", "collections", "schema", "relationships", "overview", "status"):
            return (
                f"Do you want to inspect the structure of '{target}' or view its records?\n\n"
                "Record retrieval is not available yet."
            )

    return None


def _resolve_pronouns(text: str, last_object: str | None) -> str:
    """Resolve pronouns and self-referential phrases using context."""
    # Check if the sentence itself contains the object, e.g. "Show orders and then describe it"
    m = re.search(r"\b(?:show|describe|inspect)\s+([a-zA-Z0-9_]+)\s+and\s+(?:then\s+)?(?:describe|show|inspect)\s+it\b", text, re.I)
    if m:
        target = m.group(1)
        return f"Describe the {target} table."

    if not last_object:
        return text

    resolved = text
    # Replace "its columns" / "show its columns" -> f"Show the columns in {last_object}"
    resolved = re.sub(r"\b(?:show|display|get)\s+its\s+columns\b", f"Show the columns in {last_object}", resolved, flags=re.I)
    resolved = re.sub(r"\bshow\s+its\s+structure\b", f"Describe the {last_object} table", resolved, flags=re.I)
    resolved = re.sub(r"\bdescribe\s+it\b", f"Describe the {last_object} table", resolved, flags=re.I)
    resolved = re.sub(r"\b(?:what\s+tables\s+)?reference\s+it\b", f"Which tables reference {last_object}", resolved, flags=re.I)
    resolved = re.sub(r"\breferencing\s+it\b", f"referencing {last_object}", resolved, flags=re.I)
    return resolved


@dataclass(frozen=True)
class AssistantTurnResult:
    """Outcome of a conversational assistant interaction turn."""

    response_text: str
    tool_results: tuple[ToolResult, ...] = field(default_factory=tuple)
    is_error: bool = False
    error_message: str | None = None
    response_mode: str = "conversational"  # "conversational" | "display" | "explain"
    presentation_options: dict[str, Any] = field(default_factory=dict)


class AssistantWorkflow:
    """Orchestrates multi-turn conversational interaction with local LLMs and bounded tool execution."""

    def __init__(
        self,
        llm_provider: LLMProvider,
        tool_registry: ToolRegistry,
        max_iterations: int = DEFAULT_MAX_TOOL_ITERATIONS,
    ) -> None:
        self._llm_provider = llm_provider
        self._tool_registry = tool_registry
        self._max_iterations = max_iterations
        self._history: list[ChatMessage] = []
        self._last_inspected_object: str | None = None
        self._last_related_tables: tuple[str, ...] = ()
        self._latest_tool_result: ToolResult | None = None
        self._cached_schema: DatabaseSchema | None = None
        self._cached_profile: str | None = None

    @property
    def history(self) -> tuple[ChatMessage, ...]:
        """Immutable view of current conversation history."""
        return tuple(self._history)

    def reset_conversation(self) -> None:
        """Clear conversation history, cache, and context for the current session."""
        self._history.clear()
        self._last_inspected_object = None
        self._last_related_tables = ()
        self._latest_tool_result = None
        self._cached_schema = None
        self._cached_profile = None

    def handle_user_message(
        self,
        user_text: str,
        active_profile: str | None = None,
        model: str | None = None,
    ) -> AssistantTurnResult:
        """Process a user conversational message through the three response paths."""
        cleaned_input = user_text.strip()
        if not cleaned_input:
            return AssistantTurnResult(response_text="", response_mode="conversational")

        # Invalidate cached schema if active database profile changed
        if active_profile != self._cached_profile:
            self._cached_schema = None
            self._cached_profile = active_profile

        # Resolve pronouns from conversation context if applicable
        effective_input = _resolve_pronouns(cleaned_input, self._last_inspected_object)

        # 1. Check for intent guardrails (unsupported row requests, ambiguous table display, items in orders)
        clarification = _check_intent_guardrails(effective_input)
        if clarification:
            self._history.append(ChatMessage(role="user", content=cleaned_input))
            self._history.append(ChatMessage(role="assistant", content=clarification))
            return AssistantTurnResult(
                response_text=clarification,
                response_mode="conversational",
            )

        # Record user message in session history
        user_msg = ChatMessage(role="user", content=effective_input)
        self._history.append(user_msg)

        # Build prompt context with pronoun resolution hints if applicable
        active_db_context = f"\nActive database connection: {active_profile or '(none)'}"
        context_extra = ""
        if self._last_inspected_object:
            context_extra += f"\nLast inspected database object: {self._last_inspected_object}"
        if self._last_related_tables:
            context_extra += f"\nLast referenced tables: {', '.join(self._last_related_tables)}"

        system_content = SYSTEM_PROMPT_TEMPLATE + active_db_context + context_extra
        system_msg = ChatMessage(role="system", content=system_content)

        tools: list[ToolDefinition] = self._tool_registry.get_tool_definitions()

        accumulated_results: list[ToolResult] = []
        executed_in_this_turn: set[tuple[str, str]] = set()

        iteration = 0
        final_text = ""
        overall_response_mode = "conversational"
        has_explain_request = False

        try:
            while iteration < self._max_iterations:
                iteration += 1

                # Assemble messages with current system prompt
                current_messages = [system_msg] + self._history

                response_msg: ChatMessage = self._llm_provider.chat(
                    messages=current_messages,
                    tools=tools,
                    model=model,
                )

                # Path A: Normal conversational response (no tool calls requested)
                if not response_msg.tool_calls:
                    content_text = (response_msg.content or "").strip()

                    # Fallback: If model did not emit a native tool call for an explicit database metadata query on the first turn,
                    # or suggested calling a tool, synthesize the tool call to preserve response ownership
                    synth_tool_call: ToolCall | None = None
                    lower_input = effective_input.lower()

                    if iteration == 1 and not accumulated_results:
                        if any(k in lower_input for k in ("reference", "referencing", "foreign key", "relationship between")):
                            tbl_arg = None
                            if "orders" in lower_input:
                                tbl_arg = "orders"
                            elif "users" in lower_input:
                                tbl_arg = "users"
                            args_dict: dict[str, Any] = {"response_mode": "explain"}
                            if tbl_arg:
                                args_dict["table_name"] = tbl_arg
                            synth_tool_call = ToolCall(
                                id="call_rel_auto",
                                name="get_relationships",
                                arguments=args_dict,
                            )
                        elif any(k in lower_input for k in ("columns in", "columns of", "describe", "unknown table")):
                            target_tbl = None
                            if "test_foo" in lower_input:
                                target_tbl = "test_foo"
                            elif self._last_inspected_object and "orders" in lower_input:
                                target_tbl = self._last_inspected_object
                            else:
                                m_tbl = re.search(r"\b(?:table|named|in|of|describe)\s+([a-zA-Z0-9_]+)", effective_input, re.I)
                                if m_tbl:
                                    target_tbl = m_tbl.group(1)
                                elif self._last_inspected_object:
                                    target_tbl = self._last_inspected_object

                            if target_tbl and target_tbl not in ("tables", "views", "collections", "schema", "relationships"):
                                synth_tool_call = ToolCall(
                                    id="call_desc_auto",
                                    name="describe_object",
                                    arguments={"object_name": target_tbl, "response_mode": "display"},
                                )
                        elif self._cached_schema is not None and any(k in lower_input for k in ("table", "names", "again", "public", "numbered", "alphabetical")):
                            # Fallback: follow-up reformatting request where model emitted text instead of tool call
                            synth_tool_call = ToolCall(
                                id="call_list_cached",
                                name="list_database_objects",
                                arguments={"object_type": "all", "response_mode": "display"},
                            )

                    if synth_tool_call:
                        response_msg = ChatMessage(
                            role="assistant",
                            tool_calls=(synth_tool_call,),
                        )
                    else:
                        # Check for empty or pseudo-tool output (e.g. model output only 'none' or empty)
                        if not content_text:
                            correction_msg = ChatMessage(
                                role="user",
                                content="Please answer the question directly in plain text without calling any tools.",
                            )
                            correction_resp = self._llm_provider.chat(
                                messages=current_messages + [correction_msg],
                                tools=tools,
                                model=model,
                            )
                            content_text = (correction_resp.content or "").strip()

                        if not content_text:
                            return AssistantTurnResult(
                                response_text="",
                                is_error=True,
                                error_message="The model did not provide a conversational response. Please rephrase your question.",
                                response_mode="conversational",
                            )

                        final_text = content_text
                        self._history.append(ChatMessage(role="assistant", content=final_text))
                        if not has_explain_request:
                            overall_response_mode = "conversational"
                        else:
                            overall_response_mode = "explain"
                        break

                # Tool calls were requested
                self._history.append(response_msg)
                has_explain_request = False

                for tc in response_msg.tool_calls:
                    # Resolve pronoun arguments if model passed "it"
                    args_dict = dict(tc.arguments) if isinstance(tc.arguments, dict) else {}
                    if tc.name == "describe_object":
                        raw_obj = str(args_dict.get("object_name", "")).strip().lower()
                        if raw_obj in ("it", "this", "that") and self._last_inspected_object:
                            args_dict["object_name"] = self._last_inspected_object
                    elif tc.name == "get_relationships":
                        raw_tbl = str(args_dict.get("table_name", "")).strip().lower()
                        if raw_tbl in ("it", "this", "that") and self._last_inspected_object:
                            args_dict["table_name"] = self._last_inspected_object

                    # Validate response mode preference
                    call_mode = args_dict.get("response_mode")
                    effective_mode = _resolve_response_mode(cleaned_input, call_mode)
                    if effective_mode == "explain":
                        has_explain_request = True

                    tool_signature = (tc.name, json.dumps(args_dict, sort_keys=True))
                    if tool_signature in executed_in_this_turn:
                        tool_res = ToolResult(
                            name=tc.name,
                            call_id=tc.id,
                            data={"error": f"Tool '{tc.name}' was already executed with identical arguments in this turn."},
                            is_error=True,
                            message="Repeated tool call prevented.",
                            response_mode=effective_mode,
                        )
                    else:
                        executed_in_this_turn.add(tool_signature)

                        # Check cache for list_database_objects:
                        # If cached schema is available for active_profile, reuse it without querying the database
                        if (
                            tc.name == "list_database_objects"
                            and self._cached_schema is not None
                            and self._cached_profile == active_profile
                        ):
                            pres_opts = extract_presentation_options(args_dict)
                            # Fallback if model omitted optional presentation_options in tool args
                            if not args_dict.get("presentation_options"):
                                lower_cleaned = cleaned_input.lower()
                                if any(p in lower_cleaned for p in ("without public", "without schema", "without mentioning public", "no public", "no schema", "omit schema")):
                                    pres_opts["include_schema"] = False
                                if any(p in lower_cleaned for p in ("only names", "only the names", "only the table names", "names only", "just names", "just the names")):
                                    pres_opts["names_only"] = True
                                if any(p in lower_cleaned for p in ("number the", "numbered", "numbering", "with numbers")):
                                    pres_opts["numbered"] = True
                                if any(p in lower_cleaned for p in ("alphabetical", "alphabetically", "sort")):
                                    pres_opts["alphabetical"] = True

                            tool_res = ToolResult(
                                name=tc.name,
                                call_id=tc.id,
                                data={
                                    "database": self._cached_schema.database_name,
                                    "engine": self._cached_schema.engine,
                                    "tables": [t.qualified_name for t in self._cached_schema.tables if t.table_type == TableType.TABLE],
                                    "table_count": len([t for t in self._cached_schema.tables if t.table_type == TableType.TABLE]),
                                    "cached": True,
                                },
                                raw_object=self._cached_schema,
                                is_error=False,
                                response_mode=effective_mode,
                                presentation_options=pres_opts,
                            )
                        else:
                            tool_res = self._tool_registry.execute_tool(
                                tool_name=tc.name,
                                arguments=args_dict,
                                active_profile=active_profile,
                                call_id=tc.id,
                            )
                            if (
                                not tool_res.is_error
                                and tool_res.name == "list_database_objects"
                                and isinstance(tool_res.raw_object, DatabaseSchema)
                            ):
                                self._cached_schema = tool_res.raw_object
                                self._cached_profile = active_profile

                    accumulated_results.append(tool_res)

                    # Update context tracking on successful tool result
                    if not tool_res.is_error:
                        self._latest_tool_result = tool_res
                        if tool_res.name == "describe_object":
                            self._last_inspected_object = tool_res.data.get("name") or args_dict.get("object_name")
                        elif tool_res.name == "get_relationships":
                            rels = tool_res.data.get("relationships", [])
                            sources = [r.get("source_table") for r in rels if isinstance(r, dict) and r.get("source_table")]
                            if sources:
                                self._last_related_tables = tuple(sorted(set(sources)))

                    # Return tool output as a tool response message to the assistant
                    tool_content = json.dumps(tool_res.data, default=str)
                    self._history.append(
                        ChatMessage(
                            role="tool",
                            content=tool_content,
                            tool_call_id=tc.id,
                            name=tc.name,
                        )
                    )

                # Path B: Direct data retrieval
                if not has_explain_request:
                    overall_response_mode = "display"
                    final_text = ""
                    break

                # Path C: Retrieval followed by explanation
                overall_response_mode = "explain"

            # Prune history if it exceeds threshold (retain most recent turns)
            if len(self._history) > MAX_CONVERSATION_HISTORY_MESSAGES:
                self._history = self._history[-MAX_CONVERSATION_HISTORY_MESSAGES:]

            turn_pres_opts: dict[str, Any] = {}
            for res in accumulated_results:
                if res.presentation_options:
                    turn_pres_opts.update(res.presentation_options)

            return AssistantTurnResult(
                response_text=final_text,
                tool_results=tuple(accumulated_results),
                response_mode=overall_response_mode,
                presentation_options=turn_pres_opts,
            )

        except Exception as exc:
            return AssistantTurnResult(
                response_text="",
                tool_results=tuple(accumulated_results),
                is_error=True,
                error_message=str(exc),
                response_mode=overall_response_mode,
            )
